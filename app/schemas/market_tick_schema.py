from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


class MarketTick(BaseModel):
    """主交易程序内部统一使用的逐笔行情快照。"""

    model_config = ConfigDict(frozen=True)

    source_event_id: str
    source: Literal["YML_FEEDHUB"] = "YML_FEEDHUB"

    order_book_id: str
    exchange_id: str
    symbol: str

    trading_day: date
    event_time: datetime
    local_recv_time: datetime | None = None
    server_time: datetime | None = None
    sequence_id: int

    last_price: Decimal | None = None
    pre_close: Decimal | None = None
    open_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None

    cumulative_volume: int
    cumulative_turnover: Decimal | None = None
    open_interest: Decimal | None = None

    bid_price_1: Decimal | None = None
    bid_volume_1: int
    ask_price_1: Decimal | None = None
    ask_volume_1: int

    raw_update_time: str | None = None
    raw_update_millisec: int | None = None
