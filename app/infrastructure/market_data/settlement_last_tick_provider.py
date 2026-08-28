import math
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.common.decimal_utils import quantize_money
from app.core.config import Settings, settings


SHANGHAI = ZoneInfo("Asia/Shanghai")
YMM_SETTLEMENT_LAST_TICK_SOURCE = "YMM_DATA_SDK_LAST_TICK"


class SettlementLastTickError(Exception):
    """结算专用数据库行情异常基类。"""


class SettlementLastTickConfigurationError(SettlementLastTickError, ValueError):
    """结算专用数据库行情配置不完整。"""


class SettlementLastTickSdkUnavailableError(SettlementLastTickError, RuntimeError):
    """结算进程无法加载数据库行情 SDK。"""


class SettlementLastTickDataError(SettlementLastTickError, RuntimeError):
    """数据库行情不足以形成可审计的结算价格。"""


@dataclass(frozen=True)
class SettlementLastTick:
    order_book_id: str
    trading_day: date
    event_time: datetime
    last_price: Decimal
    source_event_id: str


@dataclass(frozen=True)
class SettlementLastTickPair:
    current: SettlementLastTick
    previous: SettlementLastTick | None


def _clean(value: Any) -> Any:
    if value is None:
        return None
    try:
        if bool(value != value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    return value


class YmmSettlementLastTickProvider:
    """为日终结算批量冻结当日和前一交易日的最后一条 Tick。"""

    def __init__(
        self,
        config: Settings = settings,
        *,
        sdk_module=None,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.token = config.ymm_data_sdk_token.strip()
        mode = config.remote_market_data_mode.strip()
        self.sdk = sdk_module
        self.mode = mode
        self.timeout_seconds = max(
            float(config.remote_market_data_timeout_seconds), 0.0
        )
        self.monotonic = monotonic
        self.sleep = sleep
        self._initialized = False
        self._lock = threading.Lock()

    def _initialize(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            if not self.token:
                raise SettlementLastTickConfigurationError(
                    "缺少 YMM_DATA_SDK_TOKEN"
                )
            if self.mode.lower() not in {"lan", "ts", "local"}:
                raise SettlementLastTickConfigurationError(
                    "REMOTE_MARKET_DATA_MODE 必须是 lan、TS 或 local"
                )
            if self.sdk is None:
                try:
                    import ymm_data_sdk as sdk_module
                except ModuleNotFoundError as exc:
                    raise SettlementLastTickSdkUnavailableError(
                        "未安装 ymm-data-sdk==0.9.4"
                    ) from exc
                self.sdk = sdk_module
            self.mode = "TS" if self.mode.lower() == "ts" else self.mode.lower()
            self.sdk.init(token=self.token, mode=self.mode)
            self._initialized = True

    @staticmethod
    def _date_value(value: Any) -> date:
        converter = getattr(value, "to_pydatetime", None)
        if callable(converter):
            value = converter()
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value).strip()
        if len(text) == 8 and text.isdigit():
            text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
        return date.fromisoformat(text[:10])

    @staticmethod
    def _event_time(value: Any) -> datetime:
        converter = getattr(value, "to_pydatetime", None)
        if callable(converter):
            value = converter()
        if not isinstance(value, datetime):
            value = datetime.fromisoformat(str(value))
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI)
        return value

    @staticmethod
    def _price(value: Any, *, code: str, trading_day: date) -> Decimal:
        value = _clean(value)
        try:
            price = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise SettlementLastTickDataError(
                f"最后 Tick 价格无法解析: {code} {trading_day.isoformat()}"
            ) from exc
        if not price.is_finite() or price <= 0:
            raise SettlementLastTickDataError(
                f"最后 Tick 价格不是有限正数: {code} {trading_day.isoformat()}"
            )
        return quantize_money(price)

    def _query(self, codes: list[str], previous_day: date, trading_day: date):
        deadline = self.monotonic() + self.timeout_seconds
        while True:
            try:
                return self.sdk.get_price(
                    codes,
                    start_date=previous_day.isoformat(),
                    end_date=trading_day.isoformat(),
                    frequency="tick",
                    fields=["last", "trading_date"],
                    adjust_type="none",
                )
            except Exception as exc:
                unavailable = type(exc).__name__ == "YMMDataUnavailableError"
                temporary = unavailable and "running" in str(exc).lower()
                if temporary and self.monotonic() < deadline:
                    self.sleep(
                        min(0.25, max(deadline - self.monotonic(), 0.0))
                    )
                    continue
                if unavailable:
                    raise SettlementLastTickDataError(str(exc)) from exc
                raise

    @staticmethod
    def _tick_from_row(code: str, row: dict[str, Any]) -> SettlementLastTick:
        tick_day = YmmSettlementLastTickProvider._date_value(row["trading_date"])
        event_time = YmmSettlementLastTickProvider._event_time(row["datetime"])
        return SettlementLastTick(
            order_book_id=code,
            trading_day=tick_day,
            event_time=event_time,
            last_price=YmmSettlementLastTickProvider._price(
                row.get("last"), code=code, trading_day=tick_day
            ),
            source_event_id=(
                f"YMM_DATA_SDK:TICK:{code}:{event_time.isoformat()}"
            ),
        )

    def fetch_many(
        self, codes: Iterable[str], trading_day: date
    ) -> dict[str, SettlementLastTickPair]:
        normalized = sorted(
            {str(code).strip().upper() for code in codes if str(code).strip()}
        )
        if not normalized:
            return {}
        self._initialize()
        previous_day = self._date_value(
            self.sdk.get_previous_trading_date(trading_day, n=1, market="cn")
        )
        frame = self._query(normalized, previous_day, trading_day)
        if frame is None or frame.empty:
            raise SettlementLastTickDataError("数据库未返回结算所需 Tick")
        flat = frame.reset_index()
        required_columns = {"order_book_id", "datetime", "trading_date", "last"}
        missing = required_columns.difference(flat.columns)
        if missing:
            raise SettlementLastTickDataError(
                "数据库 Tick 缺少字段: " + ", ".join(sorted(missing))
            )

        rows_by_contract_day: dict[tuple[str, date], list[dict[str, Any]]] = {}
        for raw in flat.to_dict(orient="records"):
            code = str(raw["order_book_id"]).strip().upper()
            tick_day = self._date_value(raw["trading_date"])
            if code not in normalized or tick_day not in {previous_day, trading_day}:
                continue
            rows_by_contract_day.setdefault((code, tick_day), []).append(raw)

        result: dict[str, SettlementLastTickPair] = {}
        for code in normalized:
            current_rows = rows_by_contract_day.get((code, trading_day), [])
            if not current_rows:
                raise SettlementLastTickDataError(
                    f"当日最后 Tick 不存在: {code} {trading_day.isoformat()}"
                )
            current = self._tick_from_row(
                code,
                max(current_rows, key=lambda item: self._event_time(item["datetime"])),
            )
            previous_rows = rows_by_contract_day.get((code, previous_day), [])
            previous = (
                self._tick_from_row(
                    code,
                    max(
                        previous_rows,
                        key=lambda item: self._event_time(item["datetime"]),
                    ),
                )
                if previous_rows
                else None
            )
            result[code] = SettlementLastTickPair(
                current=current,
                previous=previous,
            )
        return result
