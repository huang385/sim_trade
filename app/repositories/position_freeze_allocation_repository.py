from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.position_freeze_allocation import PositionFreezeAllocation


class PositionFreezeAllocationRepository:
    """平仓订单逐笔冻结分配仓储，不管理事务。"""

    @staticmethod
    def add(db: Session, allocation: PositionFreezeAllocation) -> None:
        db.add(allocation)

    @staticmethod
    def list_by_order(
        db: Session,
        order_id: str,
    ) -> Sequence[PositionFreezeAllocation]:
        statement = (
            select(PositionFreezeAllocation)
            .where(PositionFreezeAllocation.order_id == order_id)
            .order_by(PositionFreezeAllocation.id)
        )
        return db.scalars(statement).all()

    @staticmethod
    def list_by_order_for_update(
        db: Session,
        order_id: str,
    ) -> Sequence[PositionFreezeAllocation]:
        """按原始FIFO分配顺序锁定订单自己的全部分配记录。"""

        statement = (
            select(PositionFreezeAllocation)
            .where(PositionFreezeAllocation.order_id == order_id)
            .order_by(PositionFreezeAllocation.id)
            .with_for_update()
        )
        return db.scalars(statement).all()
