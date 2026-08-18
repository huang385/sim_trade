from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CashSecurityPnlBasisMigrationAudit(Base):
    """Immutable audit row for the safe 0029 historical bucket backfill."""

    __tablename__ = "cash_security_pnl_basis_migration_audit"

    position_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    previous_daily_pnl_base_cost: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
