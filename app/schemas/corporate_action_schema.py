"""Validated API contracts for cash-security corporate actions."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CorporateActionComponentCreate(BaseModel):
    component_type: str = Field(min_length=1, max_length=32)
    base_quantity: Decimal = Field(gt=0)
    cash_amount: Decimal = Field(default=Decimal("0"), ge=0)
    share_ratio: Decimal = Field(default=Decimal("0"), ge=0)
    rights_ratio: Decimal = Field(default=Decimal("0"), ge=0)
    subscription_price: Decimal = Field(default=Decimal("0"), ge=0)
    withholding_tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    cash_in_lieu_price: Decimal = Field(default=Decimal("0"), ge=0)
    rounding_rule: str = Field(default="FLOOR", max_length=32)
    currency: str = Field(default="CNY", min_length=1, max_length=8)


class CorporateActionImportRequest(BaseModel):
    instrument_id: int = Field(gt=0)
    source_action_id: str = Field(min_length=1, max_length=128)
    action_version: int = Field(default=1, ge=1)
    data_source: str = Field(min_length=1, max_length=32)
    announcement_date: date | None = None
    record_date: date | None = None
    ex_date: date | None = None
    payment_date: date | None = None
    listing_date: date | None = None
    subscription_start_date: date | None = None
    subscription_end_date: date | None = None
    components: list[CorporateActionComponentCreate] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dates(self):
        if self.record_date and self.ex_date and self.ex_date < self.record_date:
            raise ValueError("ex_date cannot precede record_date")
        if self.subscription_start_date and self.subscription_end_date and self.subscription_end_date < self.subscription_start_date:
            raise ValueError("subscription_end_date cannot precede subscription_start_date")
        return self


class RightsSubscriptionRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    volume: int = Field(gt=0)
    client_request_id: str = Field(min_length=1, max_length=128)


class PriceAdjustmentFactorCreate(BaseModel):
    trading_day: date
    raw_previous_close: Decimal = Field(gt=0)
    official_ex_reference_price: Decimal = Field(gt=0)
    source_event_id: str = Field(min_length=1, max_length=128)
    data_source: str = Field(min_length=1, max_length=32)


class HistoricalPriceBar(BaseModel):
    trading_day: date
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None


class AdjustedPriceBarsRequest(BaseModel):
    instrument_id: int = Field(gt=0)
    adjustment_mode: Literal["RAW", "FORWARD", "BACKWARD"] = "RAW"
    bars: list[HistoricalPriceBar] = Field(min_length=1)


class AdjustedPriceBar(HistoricalPriceBar):
    adjustment_mode: Literal["RAW", "FORWARD", "BACKWARD"]
    adjustment_multiplier: Decimal


class CorporateActionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action_id: str
    instrument_id: int
    exchange_id: str
    order_book_id: str
    action_version: int
    status: str
    record_date: date | None
    ex_date: date | None
    payment_date: date | None
    listing_date: date | None
    subscription_start_date: date | None
    subscription_end_date: date | None
    source_action_id: str
    data_source: str
    created_at: datetime
    updated_at: datetime


class CorporateActionEntitlementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    entitlement_id: str
    action_id: str
    component_id: str
    account_id: str
    position_id: str
    record_quantity: int
    entitled_cash_gross: Decimal
    withholding_tax: Decimal
    entitled_cash_net: Decimal
    entitled_share_volume: int
    subscribed_volume: int
    pending_share_volume: int
    credited_share_volume: int
    status: str
    created_at: datetime
    updated_at: datetime
