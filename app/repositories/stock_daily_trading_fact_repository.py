from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.stock_daily_trading_fact import StockDailyTradingFact


class StockDailyTradingFactRepository:
    """股票逐日交易事实仓储；同步写入由调用方提交事务。"""

    @staticmethod
    def get(
        db: Session,
        *,
        instrument_id: int,
        trading_day: date,
    ) -> StockDailyTradingFact | None:
        return db.scalar(
            select(StockDailyTradingFact).where(
                StockDailyTradingFact.instrument_id == instrument_id,
                StockDailyTradingFact.trading_day == trading_day,
            )
        )

    @staticmethod
    def list_by_instrument_ids_and_trading_day(
        db: Session,
        *,
        instrument_ids: Sequence[int],
        trading_day: date,
    ) -> Sequence[StockDailyTradingFact]:
        ids = sorted(set(instrument_ids))
        if not ids:
            return []
        return db.scalars(
            select(StockDailyTradingFact)
            .where(
                StockDailyTradingFact.instrument_id.in_(ids),
                StockDailyTradingFact.trading_day == trading_day,
            )
            .order_by(StockDailyTradingFact.instrument_id)
        ).all()

    @staticmethod
    def upsert(
        db: Session,
        *,
        instrument_id: int,
        trading_day: date,
        previous_close: Decimal,
        upper_limit_price: Decimal | None,
        lower_limit_price: Decimal | None,
        is_suspended: bool,
        is_special_treatment: bool,
        is_tradeable: bool,
        source_event_id: str,
        data_source: str,
        synced_at: datetime,
        updated_at: datetime,
    ) -> None:
        statement = insert(StockDailyTradingFact).values(
            instrument_id=instrument_id,
            trading_day=trading_day,
            previous_close=previous_close,
            upper_limit_price=upper_limit_price,
            lower_limit_price=lower_limit_price,
            is_suspended=is_suspended,
            is_special_treatment=is_special_treatment,
            is_tradeable=is_tradeable,
            source_event_id=source_event_id,
            data_source=data_source,
            synced_at=synced_at,
            updated_at=updated_at,
        )
        db.execute(
            statement.on_conflict_do_update(
                constraint="uq_stock_daily_trading_fact_instrument_day",
                set_={
                    "previous_close": statement.excluded.previous_close,
                    "upper_limit_price": statement.excluded.upper_limit_price,
                    "lower_limit_price": statement.excluded.lower_limit_price,
                    "is_suspended": statement.excluded.is_suspended,
                    "is_special_treatment": statement.excluded.is_special_treatment,
                    "is_tradeable": statement.excluded.is_tradeable,
                    "source_event_id": statement.excluded.source_event_id,
                    "data_source": statement.excluded.data_source,
                    "synced_at": statement.excluded.synced_at,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )
