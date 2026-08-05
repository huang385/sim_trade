from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class RiskEvent(Base):
    """账户风险状态及自动处置的不可变审计记录。"""

    __tablename__ = "risk_event"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "business_version", name="uq_risk_event_account_version"
        ),
        Index("ix_risk_event_account_created", "account_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_state: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_reason: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    business_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
