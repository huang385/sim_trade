from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.fee_rule import FeeRule
from app.models.fee_rule_daily import FeeRuleDaily


class FeeRuleRepository:
    """
    手续费规则数据库仓储。
    """

    @staticmethod
    def get_current(
        db: Session,
        exchange_id: str,
        symbol: str,
    ) -> FeeRule | None:
        statement = select(FeeRule).where(
            FeeRule.exchange_id == exchange_id,
            FeeRule.symbol == symbol,
        )

        return db.scalar(statement)

    @staticmethod
    def list_current(
        db: Session,
        exchange_id: str | None = None,
    ) -> Sequence[FeeRule]:
        statement = select(FeeRule)

        if exchange_id is not None:
            statement = statement.where(
                FeeRule.exchange_id == exchange_id
            )

        statement = statement.order_by(
            FeeRule.exchange_id,
            FeeRule.symbol,
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
        commission_type: str,
        open_commission: Decimal,
        close_commission: Decimal,
        close_today_commission: Decimal,
        discount_rate: Decimal | None,
        data_source: str,
        synced_at: datetime,
        updated_at: datetime,
    ) -> None:
        statement = insert(FeeRule).values(
            order_book_id=order_book_id,
            symbol=symbol,
            exchange_id=exchange_id,
            trading_day=trading_day,
            commission_type=commission_type,
            open_commission=open_commission,
            close_commission=close_commission,
            close_today_commission=close_today_commission,
            discount_rate=discount_rate,
            data_source=data_source,
            synced_at=synced_at,
            updated_at=updated_at,
        )

        statement = statement.on_conflict_do_update(
            constraint="uq_fee_rule_exchange_symbol",
            set_={
                "order_book_id": statement.excluded.order_book_id,
                "trading_day": statement.excluded.trading_day,
                "commission_type": (
                    statement.excluded.commission_type
                ),
                "open_commission": (
                    statement.excluded.open_commission
                ),
                "close_commission": (
                    statement.excluded.close_commission
                ),
                "close_today_commission": (
                    statement.excluded.close_today_commission
                ),
                "discount_rate": statement.excluded.discount_rate,
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
    ) -> FeeRuleDaily | None:
        statement = select(FeeRuleDaily).where(
            FeeRuleDaily.trading_day == trading_day,
            FeeRuleDaily.exchange_id == exchange_id,
            FeeRuleDaily.symbol == symbol,
        )

        return db.scalar(statement)

    @staticmethod
    def list_daily(
        db: Session,
        trading_day: date,
        exchange_id: str | None = None,
    ) -> Sequence[FeeRuleDaily]:
        statement = select(FeeRuleDaily).where(
            FeeRuleDaily.trading_day == trading_day
        )

        if exchange_id is not None:
            statement = statement.where(
                FeeRuleDaily.exchange_id == exchange_id
            )

        statement = statement.order_by(
            FeeRuleDaily.exchange_id,
            FeeRuleDaily.symbol,
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
        commission_type: str,
        open_commission: Decimal,
        close_commission: Decimal,
        close_today_commission: Decimal,
        discount_rate: Decimal | None,
        data_source: str,
        sync_batch_id: str | None,
        synced_at: datetime,
        updated_at: datetime,
    ) -> None:
        statement = insert(FeeRuleDaily).values(
            order_book_id=order_book_id,
            symbol=symbol,
            exchange_id=exchange_id,
            trading_day=trading_day,
            commission_type=commission_type,
            open_commission=open_commission,
            close_commission=close_commission,
            close_today_commission=close_today_commission,
            discount_rate=discount_rate,
            data_source=data_source,
            sync_batch_id=sync_batch_id,
            synced_at=synced_at,
            updated_at=updated_at,
        )

        statement = statement.on_conflict_do_update(
            constraint="uq_fee_rule_daily_exchange_symbol_day",
            set_={
                "order_book_id": statement.excluded.order_book_id,
                "commission_type": (
                    statement.excluded.commission_type
                ),
                "open_commission": (
                    statement.excluded.open_commission
                ),
                "close_commission": (
                    statement.excluded.close_commission
                ),
                "close_today_commission": (
                    statement.excluded.close_today_commission
                ),
                "discount_rate": statement.excluded.discount_rate,
                "data_source": statement.excluded.data_source,
                "sync_batch_id": (
                    statement.excluded.sync_batch_id
                ),
                "synced_at": statement.excluded.synced_at,
                "updated_at": statement.excluded.updated_at,
            },
        )

        db.execute(statement)