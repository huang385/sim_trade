from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class CashSecurityOrderFeeAccumulator(Base):
    """订单范围费用的已收金额事实，保证最小收费只累计一次。"""

    __tablename__ = "cash_security_order_fee_accumulator"
    __table_args__ = (
        UniqueConstraint("order_id", "fee_type", name="uq_cash_fee_acc_order_type"),
        Index("ix_cash_fee_acc_order", "order_id"),
        CheckConstraint("cumulative_volume >= 0", name="ck_cash_fee_acc_volume_nonnegative"),
        CheckConstraint("cumulative_turnover >= 0", name="ck_cash_fee_acc_turnover_nonnegative"),
        CheckConstraint("charged_fee >= 0", name="ck_cash_fee_acc_charged_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id", ondelete="RESTRICT"), nullable=False)
    fee_type: Mapped[str] = mapped_column(String(32), nullable=False)
    cumulative_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cumulative_turnover: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    charged_fee: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
