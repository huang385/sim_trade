from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.enums.reference_data_enums import StockPriceLimitType


class StockTradingRuleCreate(BaseModel):
    """写入股票 Instrument 级交易规则的请求结构。"""

    instrument_id: int = Field(gt=0)
    buy_lot_size: int = Field(gt=0)
    buy_volume_must_be_multiple: bool = True
    sell_min_unit: int = Field(gt=0)
    sell_odd_lot_allowed: bool = True
    settlement_days: int = Field(ge=0)
    price_limit_type: StockPriceLimitType
    normal_price_limit_ratio: Decimal | None = Field(default=None, gt=0)
    special_price_limit_ratio: Decimal | None = Field(default=None, gt=0)
    price_cage_enabled: bool = False
    rule_version: str = Field(min_length=1, max_length=64)
    effective_from: date
    effective_to: date | None = None
    data_source: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_effective_period(self):
        if (
            self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("effective_to 不能早于 effective_from")
        return self


class StockTradingRuleResponse(StockTradingRuleCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
