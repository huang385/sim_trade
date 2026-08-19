"""Read raw daily OHLC bars from YMM/RQData for display queries."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.core.config import Settings, settings
from app.infrastructure.market_data.database_snapshot_client import (
    DatabaseSnapshotConfigurationError,
    DatabaseSnapshotSdkUnavailableError,
)


def _day(value: Any) -> date:
    converter = getattr(value, "to_pydatetime", None)
    if callable(converter):
        value = converter()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class YmmHistoricalPriceClient:
    """Raw, unadjusted source bars; adjustment belongs to the read service."""

    def __init__(self, config: Settings = settings, *, sdk_module=None) -> None:
        token = config.ymm_data_sdk_token.strip()
        mode = config.remote_market_data_mode.strip()
        if not token:
            raise DatabaseSnapshotConfigurationError("Missing YMM_DATA_SDK_TOKEN")
        if mode.lower() not in {"lan", "ts", "local"}:
            raise DatabaseSnapshotConfigurationError("REMOTE_MARKET_DATA_MODE is invalid")
        if sdk_module is None:
            try:
                import ymm_data_sdk as sdk_module
            except ModuleNotFoundError as exc:
                raise DatabaseSnapshotSdkUnavailableError("ymm-data-sdk is not installed") from exc
        self.sdk = sdk_module
        self.token = token
        self.mode = "TS" if mode.lower() == "ts" else mode.lower()
        self.initialized = False

    def _initialize(self) -> None:
        if not self.initialized:
            self.sdk.init(token=self.token, mode=self.mode)
            self.initialized = True

    def fetch_daily_bars(
        self, order_book_id: str, *, start_date: date, end_date: date
    ) -> list[dict]:
        self._initialize()
        frame = self.sdk.get_price(
            [order_book_id], start_date=start_date, end_date=end_date,
            frequency="1d", fields=["open", "high", "low", "close"],
            adjust_type="none",
        )
        if frame is None or frame.empty:
            return []
        rows = frame.reset_index().to_dict("records")
        result = []
        for row in rows:
            value = row.get("datetime", row.get("date"))
            if value is None:
                continue
            result.append({
                "trading_day": _day(value),
                "open": Decimal(str(row["open"])),
                "high": Decimal(str(row["high"])),
                "low": Decimal(str(row["low"])),
                "close": Decimal(str(row["close"])),
            })
        return sorted(result, key=lambda item: item["trading_day"])
