from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base
from app.enums.order_enums import OutboxStatus


class OutboxEvent(Base):
    """
    事务 Outbox 事件表。

    订单服务先把业务数据和待发送事件写入同一个 PostgreSQL 事务，独立
    Worker 再异步把事件发布到 Redis Stream。这样能够避免“订单已经
    提交但 Redis 消息丢失”以及“消息已经发出但订单事务回滚”两类不一致。

    payload 在 PostgreSQL 中使用 JSONB；JSON 变体仅用于 SQLite 单元测试，
    不改变生产数据库的字段类型。
    """

    __tablename__ = "outbox_event"

    __table_args__ = (
        UniqueConstraint(
            "event_id",
            name="uq_outbox_event_event_id",
        ),
        Index(
            "ix_outbox_event_pending_scan",
            "status",
            "next_retry_at",
            "id",
        ),
        Index(
            "ix_outbox_event_aggregate",
            "aggregate_type",
            "aggregate_id",
        ),
    )

    # 数据库内部自增主键，只用于稳定排序和批量扫描
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 全局唯一事件编号，由业务服务生成并用于下游幂等消费
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 聚合根类型及编号；订单接受事件固定为 ORDER + order_id
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 事件类型，例如 ORDER_ACCEPTED
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # 事件快照。Decimal、日期、时间和枚举在写入前转换为稳定字符串。
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    )

    # 发布状态和重试控制字段
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=OutboxStatus.PENDING.value,
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 生命周期时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
