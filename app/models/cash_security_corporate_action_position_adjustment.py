"""Immutable, replayable position effects of a cash-security corporate action."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class CashSecurityCorporateActionPositionAdjustment(Base):
    """One authoritative position mutation, independent of Trade facts.

    ``Position`` remains the current aggregate projection.  This append-only
    table records why it changed, so a settlement replay can validate and
    deterministically consume listed shares, splits and maturity retirements
    without inventing a historical trade.
    """

    __tablename__ = "cash_security_corporate_action_position_adjustment"
    __table_args__ = (
        UniqueConstraint("adjustment_id", name="uq_cash_corporate_adjustment_id"),
        UniqueConstraint("idempotency_key", name="uq_cash_corporate_adjustment_idempotency"),
        Index("ix_cash_corporate_adjustment_position_day", "position_id", "effective_trading_day"),
        Index("ix_cash_corporate_adjustment_detail_day", "position_detail_id", "effective_trading_day"),
        Index("ix_cash_corporate_adjustment_action_component", "action_id", "component_id"),
        Index("ix_cash_corporate_adjustment_account_day", "account_id", "effective_trading_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    adjustment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("cash_security_corporate_action.action_id", ondelete="RESTRICT"),
        nullable=False,
    )
    action_version: Mapped[int] = mapped_column(Integer, nullable=False)
    component_id: Mapped[str] = mapped_column(
        ForeignKey("cash_security_corporate_action_component.component_id", ondelete="RESTRICT"),
        nullable=False,
    )
    entitlement_id: Mapped[str] = mapped_column(
        ForeignKey("cash_security_corporate_action_entitlement.entitlement_id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("account.account_id", ondelete="RESTRICT"), nullable=False
    )
    position_id: Mapped[str] = mapped_column(
        ForeignKey("position.position_id", ondelete="RESTRICT"), nullable=False
    )
    # Some effects apply to the aggregate (for example a pre-listing right),
    # while listed shares can additionally name their generated logical lot.
    position_detail_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adjustment_type: Mapped[str] = mapped_column(String(48), nullable=False)
    effective_trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    business_version: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(192), nullable=False)

    total_volume_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    today_volume_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    yesterday_volume_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_volume_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_volume_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    frozen_volume_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settlement_locked_volume_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    position_cost_delta: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )
    daily_pnl_base_cost_delta: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )
    average_open_price_after: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6), nullable=True
    )
    # Split/reverse-split ratios and before/after quantities are retained here
    # for audit and deterministic replay without changing original Trades.
    replay_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
