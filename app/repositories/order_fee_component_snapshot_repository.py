from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order_fee_component_snapshot import OrderFeeComponentSnapshot


class OrderFeeComponentSnapshotRepository:
    """订单手续费组件快照仓储；不管理事务。"""

    @staticmethod
    def add_all(
        db: Session,
        snapshots: Sequence[OrderFeeComponentSnapshot],
    ) -> None:
        db.add_all(snapshots)

    @staticmethod
    def list_by_order_ids(
        db: Session,
        order_ids: Sequence[str],
    ) -> dict[str, list[OrderFeeComponentSnapshot]]:
        if not order_ids:
            return {}
        rows = db.scalars(
            select(OrderFeeComponentSnapshot)
            .where(OrderFeeComponentSnapshot.order_id.in_(tuple(order_ids)))
            .order_by(OrderFeeComponentSnapshot.order_id, OrderFeeComponentSnapshot.id)
        ).all()
        grouped: dict[str, list[OrderFeeComponentSnapshot]] = {}
        for row in rows:
            grouped.setdefault(row.order_id, []).append(row)
        return grouped
