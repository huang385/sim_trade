from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums.risk_enums import LiquidationTaskStatus
from app.models.liquidation_task import LiquidationTask
from app.models.risk_event import RiskEvent
from app.models.order import Order
from app.enums.order_enums import OrderStatus
from app.enums.risk_enums import OrderSource


ACTIVE_TASK_STATUSES = (
    LiquidationTaskStatus.PENDING.value,
    LiquidationTaskStatus.LIQUIDATING.value,
)


class RiskRepository:
    """风险审计和强平任务仓储；事务边界始终由Service控制。"""

    @staticmethod
    def add_event(db: Session, event: RiskEvent) -> None:
        db.add(event)

    @staticmethod
    def add_task(db: Session, task: LiquidationTask) -> None:
        db.add(task)

    @staticmethod
    def get_active_task_for_update(
        db: Session, account_id: str
    ) -> LiquidationTask | None:
        return db.scalar(
            select(LiquidationTask)
            .where(
                LiquidationTask.account_id == account_id,
                LiquidationTask.status.in_(ACTIVE_TASK_STATUSES),
            )
            .order_by(LiquidationTask.id)
            .with_for_update()
        )

    @staticmethod
    def get_task_for_update(db: Session, task_id: str) -> LiquidationTask | None:
        return db.scalar(
            select(LiquidationTask)
            .where(LiquidationTask.task_id == task_id)
            .with_for_update()
        )

    @staticmethod
    def get_task(db: Session, task_id: str) -> LiquidationTask | None:
        """无锁读取任务归属；业务随后必须按Account→Task顺序重新锁定校验。"""

        return db.scalar(
            select(LiquidationTask).where(LiquidationTask.task_id == task_id)
        )

    @staticmethod
    def list_recoverable_tasks(
        db: Session, *, limit: int
    ) -> Sequence[LiquidationTask]:
        return db.scalars(
            select(LiquidationTask)
            .where(LiquidationTask.status.in_(ACTIVE_TASK_STATUSES))
            .order_by(LiquidationTask.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()

    @staticmethod
    def list_events_by_account(
        db: Session, account_id: str, *, limit: int = 100
    ) -> Sequence[RiskEvent]:
        return db.scalars(
            select(RiskEvent)
            .where(RiskEvent.account_id == account_id)
            .order_by(RiskEvent.id.desc())
            .limit(limit)
        ).all()

    @staticmethod
    def list_tasks_by_account(
        db: Session, account_id: str, *, limit: int = 100
    ) -> Sequence[LiquidationTask]:
        return db.scalars(
            select(LiquidationTask)
            .where(LiquidationTask.account_id == account_id)
            .order_by(LiquidationTask.id.desc())
            .limit(limit)
        ).all()

    @staticmethod
    def count_filled_liquidation_orders(db: Session) -> int:
        """返回数据库中已成交强平订单总数，供低频监控指标对账。"""

        return int(
            db.scalar(
                select(func.count(Order.id)).where(
                    Order.order_source == OrderSource.LIQUIDATION.value,
                    Order.status == OrderStatus.FILLED.value,
                )
            )
            or 0
        )
