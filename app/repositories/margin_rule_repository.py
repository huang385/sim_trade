from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.margin_rule import MarginRule
from app.models.margin_rule_daily import MarginRuleDaily


class MarginRuleRepository:
    """
    保证金规则数据库仓储。

    Repository不负责commit和rollback。
    """

    @staticmethod
    def get_current(
        db: Session,
        exchange_id: str,
        symbol: str,
    ) -> MarginRule | None:
        statement = select(MarginRule).where(
            MarginRule.exchange_id == exchange_id,
            MarginRule.symbol == symbol,
        )

        return db.scalar(statement)

    @staticmethod
    def list_current(
        db: Session,
        exchange_id: str | None = None,
    ) -> Sequence[MarginRule]:
        statement = select(MarginRule)

        if exchange_id is not None:
            statement = statement.where(
                MarginRule.exchange_id == exchange_id
            )

        statement = statement.order_by(
            MarginRule.exchange_id,
            MarginRule.symbol,
        )

        return db.scalars(statement).all()

    @staticmethod
    def upsert_current(
        db: Session,
        *,
        order_book_id: str,
        symbol: str,
        exchange_id: str,
        trading_day: date,
        long_margin_rate: Decimal,
        short_margin_rate: Decimal,
        min_margin_rate: Decimal | None,
        data_source: str,
        synced_at: datetime,
        updated_at: datetime,
    ) -> None:
        statement = insert(MarginRule).values(
            order_book_id=order_book_id,
            symbol=symbol,
            exchange_id=exchange_id,
            trading_day=trading_day,
            long_margin_rate=long_margin_rate,
            short_margin_rate=short_margin_rate,
            min_margin_rate=min_margin_rate,
            data_source=data_source,
            synced_at=synced_at,
            updated_at=updated_at,
        )

        statement = statement.on_conflict_do_update(
            constraint="uq_margin_rule_exchange_symbol",
            set_={
                "order_book_id": statement.excluded.order_book_id,
                "trading_day": statement.excluded.trading_day,
                "long_margin_rate": (
                    statement.excluded.long_margin_rate
                ),
                "short_margin_rate": (
                    statement.excluded.short_margin_rate
                ),
                "min_margin_rate": (
                    statement.excluded.min_margin_rate
                ),
                "data_source": statement.excluded.data_source,
                "synced_at": statement.excluded.synced_at,
                "updated_at": statement.excluded.updated_at,
            },
        )

        db.execute(statement)

    @staticmethod
    def get_daily(
        db: Session,
        trading_day: date,
        exchange_id: str,
        symbol: str,
    ) -> MarginRuleDaily | None:
        statement = select(MarginRuleDaily).where(
            MarginRuleDaily.trading_day == trading_day,
            MarginRuleDaily.exchange_id == exchange_id,
            MarginRuleDaily.symbol == symbol,
        )

        return db.scalar(statement)

    @staticmethod
    def list_daily(
        db: Session,
        trading_day: date,
        exchange_id: str | None = None,
    ) -> Sequence[MarginRuleDaily]:
        statement = select(MarginRuleDaily).where(
            MarginRuleDaily.trading_day == trading_day
        )

        if exchange_id is not None:
            statement = statement.where(
                MarginRuleDaily.exchange_id == exchange_id
            )

        statement = statement.order_by(
            MarginRuleDaily.exchange_id,
            MarginRuleDaily.symbol,
        )

        return db.scalars(statement).all()

    @staticmethod
    def upsert_daily(
        db: Session,
        *,
        order_book_id: str,
        symbol: str,
        exchange_id: str,
        trading_day: date,
        long_margin_rate: Decimal,
        short_margin_rate: Decimal,
        min_margin_rate: Decimal | None,
        data_source: str,
        sync_batch_id: str | None,
        synced_at: datetime,
        updated_at: datetime,
    ) -> None:
        statement = insert(MarginRuleDaily).values(
            order_book_id=order_book_id,
            symbol=symbol,
            exchange_id=exchange_id,
            trading_day=trading_day,
            long_margin_rate=long_margin_rate,
            short_margin_rate=short_margin_rate,
            min_margin_rate=min_margin_rate,
            data_source=data_source,
            sync_batch_id=sync_batch_id,
            synced_at=synced_at,
            updated_at=updated_at,
        )

        statement = statement.on_conflict_do_update(
            constraint=(
                "uq_margin_rule_daily_exchange_symbol_day"
            ),
            set_={
                "order_book_id": statement.excluded.order_book_id,
                "long_margin_rate": (
                    statement.excluded.long_margin_rate
                ),
                "short_margin_rate": (
                    statement.excluded.short_margin_rate
                ),
                "min_margin_rate": (
                    statement.excluded.min_margin_rate
                ),
                "data_source": statement.excluded.data_source,
                "sync_batch_id": (
                    statement.excluded.sync_batch_id
                ),
                "synced_at": statement.excluded.synced_at,
                "updated_at": statement.excluded.updated_at,
            },
        )

        db.execute(statement)