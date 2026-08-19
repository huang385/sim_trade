from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class CashSecurityCorporateAction(Base):
    """公告级公司行为主事件。

    一条记录只描述“哪只证券在什么日期发生什么事件”，不属于任何客户；
    客户归属和登记数量必须在登记日后由 entitlement 表冻结。
    """
    __tablename__ = "cash_security_corporate_action"
    __table_args__ = (
        UniqueConstraint("action_id", name="uq_cash_corporate_action_id"),
        UniqueConstraint("source_action_id", "action_version", name="uq_cash_corporate_action_source_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 内部稳定业务编号；不使用外部公告编号，以便同一来源改版可保留审计轨迹。
    action_id: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id", ondelete="RESTRICT"), nullable=False, index=True)
    exchange_id: Mapped[str] = mapped_column(String(32), nullable=False)
    order_book_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 外部数据修订必须新建版本，已确认/已执行事实不能原地覆盖。
    action_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT", index=True)
    announcement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # record_date 仅用于日结屏障后的权益快照，不能拿次日实时持仓补算。
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    ex_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    subscription_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    subscription_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_action_id: Mapped[str] = mapped_column(String(128), nullable=False)
    data_source: Mapped[str] = mapped_column(String(32), nullable=False)
    # 来源载荷与哈希一起保存：重复投递幂等，不同内容的同版本导入必须失败。
    source_payload_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    source_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
