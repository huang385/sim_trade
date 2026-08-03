from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class InstrumentMarketDataMapping(Base):
    """
    内部合约与外部行情源代码映射。

    同一个Instrument可以在不同数据源使用不同代码；同一数据源中的
    外部代码只能映射到一个内部合约，避免行情被写入错误持仓。
    """

    __tablename__ = "instrument_market_data_mapping"
    __table_args__ = (
        UniqueConstraint(
            "data_source",
            "market_data_code",
            name="uq_market_mapping_source_code",
        ),
        UniqueConstraint(
            "instrument_id",
            "data_source",
            name="uq_market_mapping_instrument_source",
        ),
        Index(
            "ix_market_mapping_instrument_enabled",
            "instrument_id",
            "is_enabled",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instrument.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    data_source: Mapped[str] = mapped_column(String(32), nullable=False)
    market_data_code: Mapped[str] = mapped_column(String(128), nullable=False)
    market_data_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="TICK",
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

