from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class CashSecurityValuationWriterFence(Base):
    """Durable resource-side epoch for the cash valuation single writer."""

    __tablename__ = "cash_security_valuation_writer_fence"

    fence_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner: Mapped[str] = mapped_column(String(256), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
