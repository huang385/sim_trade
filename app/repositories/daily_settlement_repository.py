from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.daily_settlement import (
    DailyAccountSettlement,
    DailyPositionSettlement,
    DailySettlementBatch,
    InstrumentSettlementPrice,
    OptionExpirySettlementDetail,
)
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.trade import Trade
from app.models.trade_position_allocation import TradePositionAllocation


class DailySettlementRepository:
    """日终事实仓储；只执行查询、加锁和 flush，绝不提交或回滚。"""

    @staticmethod
    def get_batch(
        db: Session, trading_day: date, *, for_update: bool = False
    ) -> DailySettlementBatch | None:
        statement = select(DailySettlementBatch).where(
            DailySettlementBatch.trading_day == trading_day
        )
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    @staticmethod
    def get_latest_batch(db: Session) -> DailySettlementBatch | None:
        return db.scalar(
            select(DailySettlementBatch)
            .order_by(DailySettlementBatch.trading_day.desc())
            .limit(1)
        )

    @staticmethod
    def get_earlier_incomplete_batch(
        db: Session, trading_day: date
    ) -> DailySettlementBatch | None:
        return db.scalar(
            select(DailySettlementBatch)
            .where(
                DailySettlementBatch.trading_day < trading_day,
                DailySettlementBatch.status != "COMPLETED",
            )
            .order_by(DailySettlementBatch.trading_day)
            .limit(1)
        )

    @staticmethod
    def add(db: Session, instance: Any) -> None:
        db.add(instance)
        db.flush()

    @staticmethod
    def list_open_calendars(db: Session, trading_day: date) -> list[dict[str, Any]]:
        rows = db.execute(
            text(
                "SELECT exchange_id, trading_day, previous_trading_day, "
                "next_trading_day, is_open, status "
                "FROM trading_calendar WHERE trading_day = :trading_day "
                "ORDER BY exchange_id"
            ),
            {"trading_day": trading_day},
        ).mappings()
        return [dict(row) for row in rows]

    @staticmethod
    def list_product_schedules(
        db: Session, trading_day: date, product_keys: set[tuple[str, str, str]]
    ) -> list[dict[str, Any]]:
        if not product_keys:
            return []
        clauses: list[str] = []
        params: dict[str, Any] = {"trading_day": trading_day}
        for index, (exchange_id, product_code, instrument_type) in enumerate(
            sorted(product_keys)
        ):
            clauses.append(
                f"(exchange_id = :exchange_{index} AND "
                f"product_code = :product_{index} AND "
                f"instrument_type = :type_{index})"
            )
            params[f"exchange_{index}"] = exchange_id
            params[f"product_{index}"] = product_code
            params[f"type_{index}"] = instrument_type
        statement = text(
            "SELECT trading_day, exchange_id, product_code, instrument_type, "
            "sessions, status, representative_order_book_id "
            "FROM product_trading_schedule WHERE trading_day = :trading_day AND ("
            + " OR ".join(clauses)
            + ") ORDER BY exchange_id, product_code, instrument_type"
        )
        return [dict(row) for row in db.execute(statement, params).mappings()]

    @staticmethod
    def list_frozen_prices(
        db: Session, trading_day: date
    ) -> Sequence[InstrumentSettlementPrice]:
        return db.scalars(
            select(InstrumentSettlementPrice)
            .where(InstrumentSettlementPrice.trading_day == trading_day)
            .order_by(InstrumentSettlementPrice.id)
        ).all()

    @staticmethod
    def lock_all_accounts(db: Session) -> Sequence[Account]:
        return db.scalars(select(Account).order_by(Account.id).with_for_update()).all()

    @staticmethod
    def lock_all_positions(db: Session) -> Sequence[Position]:
        return db.scalars(select(Position).order_by(Position.id).with_for_update()).all()

    @staticmethod
    def lock_position_details_through_day(
        db: Session, trading_day: date
    ) -> Sequence[PositionDetail]:
        return db.scalars(
            select(PositionDetail)
            .where(PositionDetail.open_trading_day <= trading_day)
            .order_by(PositionDetail.id)
            .with_for_update()
        ).all()

    @staticmethod
    def list_trades_through_day(db: Session, trading_day: date) -> Sequence[Trade]:
        return db.scalars(
            select(Trade)
            .where(Trade.trading_day <= trading_day)
            .order_by(Trade.trade_time, Trade.id)
        ).all()

    @staticmethod
    def list_replay_order_book_ids(
        db: Session, trading_day: date
    ) -> Sequence[str]:
        closed = (
            select(
                TradePositionAllocation.position_detail_id.label("detail_id"),
                func.sum(TradePositionAllocation.close_volume).label("closed_volume"),
                func.max(TradePositionAllocation.close_trading_day).label("last_close_day"),
            )
            .where(TradePositionAllocation.close_trading_day <= trading_day)
            .group_by(TradePositionAllocation.position_detail_id)
            .subquery()
        )
        detail_codes = db.scalars(
            select(PositionDetail.order_book_id)
            .outerjoin(closed, closed.c.detail_id == PositionDetail.position_detail_id)
            .where(
                PositionDetail.open_trading_day <= trading_day,
                or_(
                    PositionDetail.original_volume
                    > func.coalesce(closed.c.closed_volume, 0),
                    PositionDetail.open_trading_day == trading_day,
                    closed.c.last_close_day == trading_day,
                ),
            )
            .distinct()
        ).all()
        trade_codes = db.scalars(
            select(Trade.order_book_id)
            .where(Trade.trading_day == trading_day)
            .distinct()
        ).all()
        return tuple(sorted(set(detail_codes) | set(trade_codes)))

    @staticmethod
    def list_trade_allocations_through_day(
        db: Session, trading_day: date
    ) -> Sequence[TradePositionAllocation]:
        return db.scalars(
            select(TradePositionAllocation)
            .where(TradePositionAllocation.close_trading_day <= trading_day)
            .order_by(TradePositionAllocation.id)
        ).all()

    @staticmethod
    def list_prior_position_settlements(
        db: Session, trading_day: date
    ) -> Sequence[DailyPositionSettlement]:
        return db.scalars(
            select(DailyPositionSettlement)
            .where(DailyPositionSettlement.trading_day < trading_day)
            .order_by(
                DailyPositionSettlement.position_id,
                DailyPositionSettlement.trading_day.desc(),
            )
        ).all()

    @staticmethod
    def list_prior_expiry_settlements(
        db: Session, trading_day: date
    ) -> Sequence[OptionExpirySettlementDetail]:
        return db.scalars(
            select(OptionExpirySettlementDetail)
            .where(OptionExpirySettlementDetail.trading_day < trading_day)
            .order_by(OptionExpirySettlementDetail.id)
        ).all()

    @staticmethod
    def list_prior_account_settlements(
        db: Session, trading_day: date
    ) -> Sequence[DailyAccountSettlement]:
        return db.scalars(
            select(DailyAccountSettlement)
            .where(
                DailyAccountSettlement.trading_day < trading_day,
                DailyAccountSettlement.status == "COMPLETED",
            )
            .order_by(DailyAccountSettlement.trading_day, DailyAccountSettlement.id)
        ).all()

    @staticmethod
    def count_completed_accounts(db: Session, batch_id: str) -> int:
        return int(
            db.scalar(
                text(
                    "SELECT count(*) FROM daily_account_settlement "
                    "WHERE batch_id = :batch_id AND status = 'COMPLETED'"
                ),
                {"batch_id": batch_id},
            )
            or 0
        )

    @staticmethod
    def count_active_orders(db: Session) -> int:
        return int(
            db.scalar(
                text(
                    "SELECT count(*) FROM orders WHERE status IN "
                    "('ACCEPTED', 'PARTIALLY_FILLED') AND remaining_volume > 0"
                )
            )
            or 0
        )
