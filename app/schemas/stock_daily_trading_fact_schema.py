from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StockDailyTradingFactUpsert(BaseModel):
    """同步股票逐日交易事实的请求结构。"""

    instrument_id: int = Field(gt=0)
    trading_day: date
    previous_close: Decimal = Field(gt=0)
    upper_limit_price: Decimal | None = Field(default=None, gt=0)
    lower_limit_price: Decimal | None = Field(default=None, gt=0)
    is_suspended: bool
    is_special_treatment: bool
    is_tradeable: bool
    source_event_id: str = Field(min_length=1, max_length=128)
    data_source: str = Field(min_length=1, max_length=32)
    synced_at: datetime

    @model_validator(mode="after")
    def validate_limit_price_pair(self):
        if (self.upper_limit_price is None) != (self.lower_limit_price is None):
            raise ValueError("涨跌停价格必须同时提供或同时为空")
        return self


class StockDailyTradingFactResponse(StockDailyTradingFactUpsert):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
