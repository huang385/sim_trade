from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class CashSecurityTradeFeeComponent(Base):
    """一笔现金证券成交的不可变费用明细。"""

    __tablename__ = "cash_security_trade_fee_component"
    __table_args__ = (
        UniqueConstraint("trade_id", "fee_type", name="uq_cash_trade_fee_component"),
        Index("ix_cash_trade_fee_component_trade", "trade_id"),
        CheckConstraint("fee_amount >= 0", name="ck_cash_trade_fee_amount_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(ForeignKey("trade.trade_id", ondelete="RESTRICT"), nullable=False)
    fee_type: Mapped[str] = mapped_column(String(32), nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
