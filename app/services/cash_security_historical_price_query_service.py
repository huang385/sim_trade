"""Historical cash-security bar query with explicit adjustment mode."""

from datetime import date
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessRuleError, ResourceNotFoundError
from app.enums.instrument_enums import CASH_SECURITY_INSTRUMENT_TYPES
from app.models.instrument import Instrument
from app.services.cash_security_price_adjustment_service import (
    AdjustmentMode,
    CashSecurityPriceAdjustmentService,
)


class HistoricalBarSource(Protocol):
    def fetch_daily_bars(
        self, order_book_id: str, *, start_date: date, end_date: date
    ) -> list[dict]: ...


class CashSecurityHistoricalPriceQueryService:
    """Keep external raw bars and database adjustment factors separately auditable."""

    def query_daily_bars(
        self, db: Session, *, source: HistoricalBarSource, order_book_id: str,
        start_date: date, end_date: date, adjustment_mode: AdjustmentMode,
    ) -> list[dict]:
        instrument = db.scalar(select(Instrument).where(
            Instrument.order_book_id == order_book_id.upper()
        ))
        if instrument is None:
            raise ResourceNotFoundError("Instrument does not exist")
        if instrument.instrument_type not in CASH_SECURITY_INSTRUMENT_TYPES:
            raise BusinessRuleError(
                "Only cash securities support corporate-action-adjusted bars",
                error_code="HISTORICAL_PRICE_PRODUCT_INVALID",
            )
        raw = source.fetch_daily_bars(
            instrument.order_book_id, start_date=start_date, end_date=end_date
        )
        return CashSecurityPriceAdjustmentService().adjust_bars(
            db, instrument_id=instrument.id, mode=adjustment_mode, bars=raw
        )
