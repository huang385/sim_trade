import math
import threading
import time
from datetime import date, datetime
from typing import Any, Iterable

from app.core.config import Settings, settings


YMM_DATABASE_SOURCE = "YMM_DATA_SDK"


class DatabaseSnapshotConfigurationError(ValueError):
    """数据库行情SDK配置不完整。"""


class DatabaseSnapshotSdkUnavailableError(RuntimeError):
    """运行环境没有安装ymm-data-sdk。"""


def _clean(value: Any) -> Any:
    """把pandas/numpy缺失值转成None，保留Decimal安全的原始标量。"""

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


class YmmDatabaseSnapshotClient:
    """低频查询新增订阅合约的最后一条已入库Tick。"""

    def __init__(
        self,
        config: Settings = settings,
        *,
        sdk_module=None,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        token = config.ymm_data_sdk_token.strip()
        mode = config.remote_market_data_mode.strip()
        if not token:
            raise DatabaseSnapshotConfigurationError(
                "缺少YMM_DATA_SDK_TOKEN"
            )
        if mode.lower() not in {"lan", "ts", "local"}:
            raise DatabaseSnapshotConfigurationError(
                "REMOTE_MARKET_DATA_MODE必须是lan、TS或local"
            )
        if sdk_module is None:
            try:
                import ymm_data_sdk as sdk_module
            except ModuleNotFoundError as exc:
                raise DatabaseSnapshotSdkUnavailableError(
                    "未安装ymm-data-sdk==0.9.1"
                ) from exc
        self.sdk = sdk_module
        self.token = token
        self.mode = "TS" if mode.lower() == "ts" else mode.lower()
        self.timeout_seconds = max(
            float(config.remote_market_data_timeout_seconds),
            0.0,
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
            self.sdk.init(token=self.token, mode=self.mode)
            self._initialized = True

    @staticmethod
    def _date_text(value: Any) -> str:
        converter = getattr(value, "to_pydatetime", None)
        if callable(converter):
            value = converter()
        if isinstance(value, datetime):
            value = value.date()
        if isinstance(value, date):
            return value.isoformat()
        text = str(value).strip()
        if len(text) == 8 and text.isdigit():
            return f"{text[:4]}-{text[4:6]}-{text[6:]}"
        return text[:10]

    def _current_trading_day(self) -> str:
        value = self.sdk.get_future_latest_trading_date()
        if value is None:
            raise RuntimeError("数据库SDK未返回当前期货交易日")
        return self._date_text(value)

    @staticmethod
    def _row_to_tick(code: str, event_time: Any, row: dict[str, Any]) -> dict:
        asks = [_clean(row.get(f"a{level}")) for level in range(1, 6)]
        bids = [_clean(row.get(f"b{level}")) for level in range(1, 6)]
        ask_volumes = [
            _clean(row.get(f"a{level}_v")) for level in range(1, 6)
        ]
        bid_volumes = [
            _clean(row.get(f"b{level}_v")) for level in range(1, 6)
        ]
        return {
            "action": "feed",
            "channel": f"tick_{code}",
            "order_book_id": code,
            "datetime": event_time,
            "trading_date": _clean(row.get("trading_date")),
            "last": _clean(row.get("last")),
            "prev_close": _clean(row.get("prev_close")),
            "open": _clean(row.get("open")),
            "high": _clean(row.get("high")),
            "low": _clean(row.get("low")),
            "volume": _clean(row.get("volume")),
            "total_turnover": _clean(row.get("total_turnover")),
            "open_interest": _clean(row.get("open_interest")),
            "ask": asks,
            "ask_vol": ask_volumes,
            "bid": bids,
            "bid_vol": bid_volumes,
            "local_recv_time": datetime.now().astimezone(),
        }

    def fetch_latest_many(self, codes: Iterable[str]) -> dict[str, dict]:
        """批量查询并按合约返回当前交易日最后一条Tick。"""

        normalized = sorted({str(code).strip().upper() for code in codes if str(code).strip()})
        if not normalized:
            return {}
        self._initialize()
        trading_day = self._current_trading_day()
        deadline = self.monotonic() + self.timeout_seconds
        while True:
            try:
                frame = self.sdk.get_price(
                    normalized,
                    start_date=trading_day,
                    end_date=trading_day,
                    frequency="tick",
                    fields=None,
                    adjust_type="none",
                )
                break
            except Exception as exc:
                temporary = (
                    type(exc).__name__ == "YMMDataUnavailableError"
                    and "running" in str(exc).lower()
                )
                if not temporary or self.monotonic() >= deadline:
                    raise
                self.sleep(min(0.25, max(deadline - self.monotonic(), 0.0)))
        if frame is None or frame.empty:
            return {}

        flat = frame.reset_index()
        if "order_book_id" not in flat.columns or "datetime" not in flat.columns:
            raise RuntimeError("数据库Tick缺少order_book_id或datetime")
        result: dict[str, dict] = {}
        for code, rows in flat.groupby("order_book_id", sort=False):
            latest = rows.sort_values("datetime").iloc[-1]
            values = latest.to_dict()
            tick = self._row_to_tick(
                str(code).strip().upper(),
                values.pop("datetime"),
                values,
            )
            if self._date_text(tick["trading_date"]) != trading_day:
                continue
            result[tick["order_book_id"]] = tick
        return result
