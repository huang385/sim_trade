from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trade_position_allocation import TradePositionAllocation


class TradePositionAllocationRepository:
    """
    平仓成交逐笔持仓明细仓储。

    Repository 只负责加入当前 Session 和查询，不计算手续费、保证金或盈亏，
    也不执行 commit/rollback；事务边界仍由成交结算 Service 管理。
    """

    @staticmethod
    def add(db: Session, item: TradePositionAllocation) -> None:
        db.add(item)

    @staticmethod
    def list_by_trade(
        db: Session,
        trade_id: str,
    ) -> Sequence[TradePositionAllocation]:
        statement = (
            select(TradePositionAllocation)
            .where(TradePositionAllocation.trade_id == trade_id)
            .order_by(TradePositionAllocation.id)
        )
        return db.scalars(statement).all()

    @staticmethod
    def list_by_order(
        db: Session,
        order_id: str,
    ) -> Sequence[TradePositionAllocation]:
        statement = (
            select(TradePositionAllocation)
            .where(TradePositionAllocation.order_id == order_id)
            .order_by(TradePositionAllocation.id)
        )
        return db.scalars(statement).all()
