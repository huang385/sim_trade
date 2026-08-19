"""Immutable requests for a rights-issue subscription."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class CashSecurityCorporateActionSubscription(Base):
    """One accepted partial subscription request.

    The entitlement remains the record-date eligibility snapshot; this table
    records each independently idempotent use of that eligibility.
    """

    __tablename__ = "cash_security_corporate_action_subscription"
    __table_args__ = (
        UniqueConstraint("entitlement_id", "client_request_id", name="uq_cash_corporate_subscription_request"),
        CheckConstraint("volume > 0 AND cash_amount >= 0", name="ck_cash_corporate_subscription_amounts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    subscription_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    entitlement_id: Mapped[str] = mapped_column(
        ForeignKey("cash_security_corporate_action_entitlement.entitlement_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    action_id: Mapped[str] = mapped_column(
        ForeignKey("cash_security_corporate_action.action_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("account.account_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    volume: Mapped[int] = mapped_column(Integer, nullable=False)
    cash_amount: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
