from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class StockDailyTradingFact(Base):
    """股票逐交易日的可交易状态及价格限制事实。"""

    __tablename__ = "stock_daily_trading_fact"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "trading_day",
            name="uq_stock_daily_trading_fact_instrument_day",
        ),
        CheckConstraint(
            "previous_close > 0",
            name="ck_stock_daily_trading_fact_previous_close_positive",
        ),
        CheckConstraint(
            "(upper_limit_price IS NULL AND lower_limit_price IS NULL) OR "
            "(upper_limit_price > 0 AND lower_limit_price > 0 AND "
            "upper_limit_price >= lower_limit_price)",
            name="ck_stock_daily_trading_fact_limit_prices_valid",
        ),
        CheckConstraint(
            "NOT is_suspended OR NOT is_tradeable",
            name="ck_stock_daily_trading_fact_suspension_not_tradeable",
        ),
        CheckConstraint(
            "source_event_id <> '' AND data_source <> ''",
            name="ck_stock_daily_trading_fact_identity_not_empty",
        ),
        Index("ix_stock_daily_trading_fact_trading_day", "trading_day"),
        Index(
            "ix_stock_daily_trading_fact_instrument_day_lookup",
            "instrument_id",
            "trading_day",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instrument.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    previous_close: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    upper_limit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6),
        nullable=True,
    )
    lower_limit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6),
        nullable=True,
    )
    is_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_special_treatment: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_tradeable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    data_source: Mapped[str] = mapped_column(String(32), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
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
