from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class LiquidationTask(Base):
    """可审计、可重启恢复的单账户强平任务。"""

    __tablename__ = "liquidation_task"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_liquidation_task_task_id"),
        # active_key仅在活动任务中保存account_id，终态改为NULL。数据库唯一约束
        # 因而可以跨进程保证同一账户最多存在一个活动任务，同时兼容SQLite测试。
        UniqueConstraint("active_key", name="uq_liquidation_task_active_key"),
        Index("ix_liquidation_task_status_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    active_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger_reason: Mapped[str] = mapped_column(String(128), nullable=False)
    trigger_snapshot: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 下单提交前先持久化幂等键。进程若在订单提交后、任务回写前崩溃，
    # 重启后仍使用同一client_order_id查询原订单，不会重复生成强平委托。
    pending_client_order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
