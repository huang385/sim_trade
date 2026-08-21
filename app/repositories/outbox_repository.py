from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.common.time_utils import utc_now
from app.enums.order_enums import OutboxStatus
from app.models.outbox_event import OutboxEvent


class OutboxRepository:
    """
    事务 Outbox 的数据库仓储。

    本类只处理事件的查询和状态修改，不连接 Redis，也不负责 commit 或
    rollback。事务边界分别由 OrderService 和发布 Worker 控制。
    """

    @staticmethod
    def create_event(
        db: Session,
        *,
        event_id: str,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict,
        created_at: datetime,
        max_retries: int = 10,
    ) -> OutboxEvent:
        """构造一个待发布事件，并加入当前数据库事务。"""

        event = OutboxEvent(
            event_id=event_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            status=OutboxStatus.PENDING.value,
            retry_count=0,
            max_retries=max_retries,
            next_retry_at=None,
            last_error=None,
            created_at=created_at,
            sent_at=None,
            updated_at=created_at,
        )
        db.add(event)
        return event

    @staticmethod
    def get_by_event_id(db: Session, event_id: str) -> OutboxEvent | None:
        """按全局事件编号查询单个事件。"""

        return db.scalar(
            select(OutboxEvent).where(OutboxEvent.event_id == event_id)
        )

    @staticmethod
    def _latest_fact_conditions(
        *,
        account_ids: Sequence[str] = (),
        position_ids: Sequence[str] = (),
        exclude_fact_reasons: Sequence[str] = (),
    ) -> list:
        """构造账户/持仓最新事实查询条件，可排除指定fact_reason的事实。

        排除使用IS DISTINCT FROM保持NULL安全：payload缺失fact_reason的历史
        事件仍参与比对，只有明确标记为排除原因的事件才被忽略。
        """

        conditions = []
        if account_ids:
            conditions.append(
                and_(
                    OutboxEvent.aggregate_type == "ACCOUNT",
                    OutboxEvent.aggregate_id.in_(tuple(account_ids)),
                    OutboxEvent.event_type.in_(
                        ("ACCOUNT_FACT_UPDATED", "ACCOUNT_UPDATED")
                    ),
                )
            )
        if position_ids:
            conditions.append(
                and_(
                    OutboxEvent.aggregate_type == "POSITION",
                    OutboxEvent.aggregate_id.in_(tuple(position_ids)),
                    OutboxEvent.event_type.in_(
                        ("POSITION_UPDATED", "POSITION_CLOSED")
                    ),
                )
            )
        if not conditions:
            return conditions
        for reason in exclude_fact_reasons:
            conditions = [
                and_(
                    condition,
                    OutboxEvent.payload["fact_reason"]
                    .as_string()
                    .is_distinct_from(reason),
                )
                for condition in conditions
            ]
        return conditions

    @staticmethod
    def list_latest_fact_versions(
        db: Session,
        *,
        account_ids: Sequence[str] = (),
        position_ids: Sequence[str] = (),
        exclude_fact_reasons: Sequence[str] = (),
    ) -> dict[tuple[str, str], str]:
        """一次集合查询返回账户和持仓最后一个事务事实Outbox编号。

        Outbox主键与业务事实同事务生成且永久单调，不会被PnL定时持久化
        改写，因此比Account/Position.updated_at更适合作为实时估值来源版本。
        """

        conditions = OutboxRepository._latest_fact_conditions(
            account_ids=account_ids,
            position_ids=position_ids,
            exclude_fact_reasons=exclude_fact_reasons,
        )
        if not conditions:
            return {}
        rows = db.execute(
            select(
                OutboxEvent.aggregate_type,
                OutboxEvent.aggregate_id,
                func.max(OutboxEvent.id),
            )
            .where(or_(*conditions))
            .group_by(
                OutboxEvent.aggregate_type,
                OutboxEvent.aggregate_id,
            )
        ).all()
        return {
            (str(aggregate_type), str(aggregate_id)): str(version)
            for aggregate_type, aggregate_id, version in rows
        }

    @staticmethod
    def list_latest_fact_created_times(
        db: Session,
        *,
        account_ids: Sequence[str] = (),
        position_ids: Sequence[str] = (),
        exclude_fact_reasons: Sequence[str] = (),
    ) -> dict[tuple[str, str], datetime]:
        """返回每个聚合最新事实（可排除指定fact_reason）的创建时间。

        与list_latest_fact_versions使用完全相同的筛选口径；先按最大id定位
        行再取其created_at，避免max(created_at)与max(id)来自不同事实。
        """

        conditions = OutboxRepository._latest_fact_conditions(
            account_ids=account_ids,
            position_ids=position_ids,
            exclude_fact_reasons=exclude_fact_reasons,
        )
        if not conditions:
            return {}
        latest = (
            select(
                OutboxEvent.aggregate_type,
                OutboxEvent.aggregate_id,
                func.max(OutboxEvent.id).label("max_id"),
            )
            .where(or_(*conditions))
            .group_by(
                OutboxEvent.aggregate_type,
                OutboxEvent.aggregate_id,
            )
            .subquery()
        )
        rows = db.execute(
            select(
                OutboxEvent.aggregate_type,
                OutboxEvent.aggregate_id,
                OutboxEvent.created_at,
            ).join(
                latest,
                and_(
                    OutboxEvent.aggregate_type
                    == latest.c.aggregate_type,
                    OutboxEvent.aggregate_id == latest.c.aggregate_id,
                    OutboxEvent.id == latest.c.max_id,
                ),
            )
        ).all()
        return {
            (str(aggregate_type), str(aggregate_id)): created_at
            for aggregate_type, aggregate_id, created_at in rows
        }

    @staticmethod
    def claim_pending_events(
        db: Session,
        *,
        batch_size: int = 100,
        now: datetime | None = None,
        processing_timeout_seconds: int = 60,
    ) -> Sequence[OutboxEvent]:
        """
        领取一批到期事件，并使用 SKIP LOCKED 支持多个 Worker 并发。

        PROCESSING 事件会设置领取租约。若 Worker 在发布过程中崩溃，租约
        到期后其他 Worker 可以重新领取，避免事件永久卡死。Redis Stream
        消费者仍应按 event_id 幂等处理，因为崩溃窗口下消息语义是至少一次。
        """

        current_time = now or utc_now()
        due_pending = and_(
            OutboxEvent.status == OutboxStatus.PENDING.value,
            or_(
                OutboxEvent.next_retry_at.is_(None),
                OutboxEvent.next_retry_at <= current_time,
            ),
        )
        expired_processing = and_(
            OutboxEvent.status == OutboxStatus.PROCESSING.value,
            OutboxEvent.next_retry_at <= current_time,
        )

        statement = (
            select(OutboxEvent)
            .where(or_(due_pending, expired_processing))
            .order_by(OutboxEvent.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        events = db.scalars(statement).all()
        lease_deadline = current_time + timedelta(
            seconds=processing_timeout_seconds
        )
        for event in events:
            event.status = OutboxStatus.PROCESSING.value
            event.next_retry_at = lease_deadline
            event.updated_at = current_time
        db.flush()
        return events

    @staticmethod
    def mark_sent(
        event: OutboxEvent,
        *,
        sent_at: datetime | None = None,
    ) -> None:
        """标记事件已经成功写入 Redis Stream。"""

        current_time = sent_at or utc_now()
        event.status = OutboxStatus.SENT.value
        event.sent_at = current_time
        event.next_retry_at = None
        event.last_error = None
        event.updated_at = current_time

    @staticmethod
    def mark_retry(
        event: OutboxEvent,
        *,
        error: str,
        now: datetime | None = None,
    ) -> None:
        """
        记录一次发布失败，并按指数退避安排下一次尝试。

        等待时间依次为 5、10、20、40 秒，最大不超过 300 秒。达到
        max_retries 后直接进入 FAILED，不再参与自动扫描。
        """

        current_time = now or utc_now()
        event.retry_count += 1
        event.last_error = error[:4000]
        event.updated_at = current_time
        if event.retry_count >= event.max_retries:
            OutboxRepository.mark_failed(
                event,
                error=error,
                failed_at=current_time,
            )
            return

        delay_seconds = min(5 * (2 ** (event.retry_count - 1)), 300)
        event.status = OutboxStatus.PENDING.value
        event.next_retry_at = current_time + timedelta(seconds=delay_seconds)

    @staticmethod
    def mark_failed(
        event: OutboxEvent,
        *,
        error: str,
        failed_at: datetime | None = None,
    ) -> None:
        """把达到重试上限的事件标记为永久失败。"""

        current_time = failed_at or utc_now()
        event.status = OutboxStatus.FAILED.value
        event.next_retry_at = None
        event.last_error = error[:4000]
        event.updated_at = current_time
