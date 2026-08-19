"""Read-side adjustment of cash-security historical bars.

Order entry, matching and valuation always use the exchange's raw price.  This
service is intentionally limited to chart/analysis read models.
"""

from datetime import date
from decimal import Decimal
from typing import Iterable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.cash_security_price_adjustment_factor import (
    CashSecurityPriceAdjustmentFactor,
)


AdjustmentMode = Literal["RAW", "FORWARD", "BACKWARD"]


class CashSecurityPriceAdjustmentService:
    """Apply persisted official ex-right factors without altering raw facts."""

    def multiplier(
        self,
        db: Session,
        *,
        instrument_id: int,
        trading_day: date,
        mode: AdjustmentMode,
    ) -> Decimal:
        if mode == "RAW":
            return Decimal("1")
        rows = db.scalars(
            select(CashSecurityPriceAdjustmentFactor)
            .where(CashSecurityPriceAdjustmentFactor.instrument_id == instrument_id)
            .order_by(
                CashSecurityPriceAdjustmentFactor.trading_day,
                CashSecurityPriceAdjustmentFactor.id,
            )
        ).all()
        multiplier = Decimal("1")
        for row in rows:
            # Forward adjustment makes pre-ex bars continuous with the newer
            # raw price; backward adjustment scales ex-and-later bars back to
            # the earlier raw-price basis.  An ex-date bar is already raw-ex.
            if mode == "FORWARD" and row.trading_day > trading_day:
                multiplier *= Decimal(row.forward_adjustment_factor)
            elif mode == "BACKWARD" and row.trading_day <= trading_day:
                multiplier *= Decimal(row.backward_adjustment_factor)
        return multiplier

    def adjust_bars(
        self,
        db: Session,
        *,
        instrument_id: int,
        mode: AdjustmentMode,
        bars: Iterable[dict],
    ) -> list[dict]:
        """Return copies of OHLC bars adjusted by the requested display mode."""
        adjusted: list[dict] = []
        for bar in bars:
            value = dict(bar)
            multiplier = self.multiplier(
                db,
                instrument_id=instrument_id,
                trading_day=value["trading_day"],
                mode=mode,
            )
            for field in ("open", "high", "low", "close"):
                if value.get(field) is not None:
                    value[field] = Decimal(value[field]) * multiplier
            value["adjustment_mode"] = mode
            value["adjustment_multiplier"] = multiplier
            adjusted.append(value)
        return adjusted
