"""API contracts for historical cash-security bars."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class HistoricalPriceBarResponse(BaseModel):
    trading_day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjustment_mode: Literal["RAW", "FORWARD", "BACKWARD"]
    adjustment_multiplier: Decimal
