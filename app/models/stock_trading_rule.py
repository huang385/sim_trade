from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class StockTradingRule(Base):
    """按股票 Instrument 维护的、带生效区间的交易规则事实。"""

    __tablename__ = "stock_trading_rule"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "rule_version",
            name="uq_stock_trading_rule_instrument_version",
        ),
        CheckConstraint(
            "buy_lot_size > 0 AND sell_min_unit > 0 AND settlement_days >= 0",
            name="ck_stock_trading_rule_volume_and_settlement_valid",
        ),
        CheckConstraint(
            "normal_price_limit_ratio IS NULL OR normal_price_limit_ratio >= 0",
            name="ck_stock_trading_rule_normal_limit_nonnegative",
        ),
        CheckConstraint(
            "special_price_limit_ratio IS NULL OR special_price_limit_ratio >= 0",
            name="ck_stock_trading_rule_special_limit_nonnegative",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_stock_trading_rule_effective_period_valid",
        ),
        CheckConstraint(
            "rule_version <> '' AND data_source <> ''",
            name="ck_stock_trading_rule_identity_not_empty",
        ),
        Index(
            "ix_stock_trading_rule_instrument_effective",
            "instrument_id",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instrument.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    buy_lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    buy_volume_must_be_multiple: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    sell_min_unit: Mapped[int] = mapped_column(Integer, nullable=False)
    sell_odd_lot_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    settlement_days: Mapped[int] = mapped_column(Integer, nullable=False)
    price_limit_type: Mapped[str] = mapped_column(String(32), nullable=False)
    normal_price_limit_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8),
        nullable=True,
    )
    special_price_limit_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8),
        nullable=True,
    )
    price_cage_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_source: Mapped[str] = mapped_column(String(32), nullable=False)
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
