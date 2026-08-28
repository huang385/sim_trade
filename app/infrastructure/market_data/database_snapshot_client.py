import math
import threading
from datetime import date, datetime
from typing import Any, Iterable

from app.core.config import Settings, settings


YMM_DATABASE_SOURCE = "YMM_DATA_SDK"


class DatabaseSnapshotConfigurationError(ValueError):
    """数据库行情 SDK 配置不完整。"""


class DatabaseSnapshotSdkUnavailableError(RuntimeError):
    """运行环境没有安装 ymm-data-sdk。"""


def _clean(value: Any) -> Any:
    """将 pandas/numpy 缺失值转成 None，保留 Decimal 安全的原始标量。"""

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
    """补取新订阅合约当前交易日最新的已落库 Tick。"""

    def __init__(
        self,
        config: Settings = settings,
        *,
        sdk_module=None,
    ) -> None:
        token = config.ymm_data_sdk_token.strip()
        mode = config.remote_market_data_mode.strip()
        if not token:
            raise DatabaseSnapshotConfigurationError(
                "缺少 YMM_DATA_SDK_TOKEN"
            )
        if mode.lower() not in {"lan", "ts", "local"}:
            raise DatabaseSnapshotConfigurationError(
                "REMOTE_MARKET_DATA_MODE 必须是 lan、TS 或 local"
            )
        if sdk_module is None:
            try:
                import ymm_data_sdk as sdk_module
            except ModuleNotFoundError as exc:
                raise DatabaseSnapshotSdkUnavailableError(
                    "未安装 ymm-data-sdk==0.9.4"
                ) from exc
        self.sdk = sdk_module
        self.token = token
        self.mode = "TS" if mode.lower() == "ts" else mode.lower()
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
        """为 SDK 快照补齐交易日，避免夜盘按自然日期被错误标记。"""

        value = self.sdk.get_future_latest_trading_date()
        if value is None:
            raise RuntimeError("数据库 SDK 未返回当前期货交易日")
        return self._date_text(value)

    @staticmethod
    def _snapshot_to_tick(
        snapshot: Any,
        *,
        code: str,
        trading_day: str,
    ) -> dict[str, Any]:
        """把 SDK 的稳定 Tick 对象转换为系统统一的行情入口报文。"""

        return {
            "action": "feed",
            "channel": f"tick_{code}",
            "order_book_id": code,
            "datetime": _clean(snapshot.datetime),
            # current_snapshot 的公开 Tick 对象不暴露 trading_date；不能从
            # datetime 推算，否则夜盘会落入自然日。使用 SDK 当前期货交易日与
            # 原有启动补快照链路保持一致。
            "trading_date": trading_day,
            "last": _clean(snapshot.last),
            "prev_close": _clean(snapshot.prev_close),
            "open": _clean(snapshot.open),
            "high": _clean(snapshot.high),
            "low": _clean(snapshot.low),
            "volume": _clean(snapshot.volume),
            "total_turnover": _clean(snapshot.total_turnover),
            "open_interest": _clean(snapshot.open_interest),
            "ask": [_clean(value) for value in snapshot.asks],
            "ask_vol": [_clean(value) for value in snapshot.ask_vols],
            "bid": [_clean(value) for value in snapshot.bids],
            "bid_vol": [_clean(value) for value in snapshot.bid_vols],
            "local_recv_time": datetime.now().astimezone(),
        }

    def fetch_latest_many(self, codes: Iterable[str]) -> dict[str, dict]:
        """批量读取当前有效交易日内每个合约最后一条已落库 Tick。"""

        normalized = sorted(
            {str(code).strip().upper() for code in codes if str(code).strip()}
        )
        if not normalized:
            return {}
        self._initialize()
        trading_day = self._current_trading_day()
        snapshots = self.sdk.current_snapshot(normalized)
        if not isinstance(snapshots, (list, tuple)):
            snapshots = [snapshots]

        result: dict[str, dict] = {}
        requested = set(normalized)
        for snapshot in snapshots:
            if not bool(getattr(snapshot, "available", False)):
                continue
            code = str(getattr(snapshot, "order_book_id", "")).strip().upper()
            if code not in requested:
                continue
            result[code] = self._snapshot_to_tick(
                snapshot,
                code=code,
                trading_day=trading_day,
            )
        return result
