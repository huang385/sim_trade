from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.enums.instrument_enums import CASH_SECURITY_INSTRUMENT_TYPES
from app.enums.reference_data_enums import StockDailyTradingFactUpsertResult
from app.models.instrument import Instrument
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
    ) -> StockDailyTradingFactUpsertResult:
        """写入单调的逐日事实，拒绝旧事件覆盖新状态。"""

        instrument = db.get(Instrument, instrument_id)
        if (
            instrument is None
            or instrument.instrument_type not in CASH_SECURITY_INSTRUMENT_TYPES
        ):
            raise ValueError(
                "现金证券逐日事实只能关联 STOCK Instrument、"
                "CONVERTIBLE_BOND Instrument 或 ETF Instrument"
            )

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
            created_at=updated_at,
            updated_at=updated_at,
        )
        inserted_id = db.scalar(
            statement.on_conflict_do_nothing(
                constraint="uq_stock_daily_trading_fact_instrument_day"
            ).returning(StockDailyTradingFact.id)
        )
        if inserted_id is not None:
            return StockDailyTradingFactUpsertResult.INSERTED

        current = db.scalar(
            select(StockDailyTradingFact)
            .where(
                StockDailyTradingFact.instrument_id == instrument_id,
                StockDailyTradingFact.trading_day == trading_day,
            )
            .with_for_update()
        )
        if current is None:
            raise RuntimeError("股票逐日事实并发写入后未找到目标记录")
        if current.source_event_id == source_event_id:
            return StockDailyTradingFactUpsertResult.DUPLICATE
        if synced_at < current.synced_at:
            return StockDailyTradingFactUpsertResult.IGNORED_STALE
        if synced_at == current.synced_at:
            return StockDailyTradingFactUpsertResult.CONFLICT_SAME_TIMESTAMP

        current.previous_close = previous_close
        current.upper_limit_price = upper_limit_price
        current.lower_limit_price = lower_limit_price
        current.is_suspended = is_suspended
        current.is_special_treatment = is_special_treatment
        current.is_tradeable = is_tradeable
        current.source_event_id = source_event_id
        current.data_source = data_source
        current.synced_at = synced_at
        current.updated_at = updated_at
        return StockDailyTradingFactUpsertResult.UPDATED
