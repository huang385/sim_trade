from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from app.common.code_utils import normalize_code
from app.enums.order_enums import OffsetFlag, OrderDirection


class MarketPreparationStatus(str, Enum):
    """期权下单前行情准备状态。"""

    NOT_REQUESTED = "NOT_REQUESTED"
    WAITING_MARKET_DATA = "WAITING_MARKET_DATA"
    READY = "READY"


class OptionMarketPrepareRequest(BaseModel):
    """为一笔计划中的期权订单准备期权及标的行情。"""

    account_id: str = Field(min_length=1, max_length=64)
    exchange_id: str = Field(min_length=1, max_length=32)
    symbol: str = Field(min_length=1, max_length=64)
    direction: OrderDirection
    offset_flag: OffsetFlag

    @field_validator("exchange_id", "symbol", mode="before")
    @classmethod
    def normalize_codes(cls, value: str) -> str:
        return normalize_code(value)


class OptionMarketPrepareResponse(BaseModel):
    """期权和标的临时订阅及最新行情就绪情况。"""

    account_id: str
    exchange_id: str
    symbol: str
    status: MarketPreparationStatus
    requested_codes: list[str]
    ready_codes: list[str]
    expires_at: datetime | None
    latest_prices_available: bool
