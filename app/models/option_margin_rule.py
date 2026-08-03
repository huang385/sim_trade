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


class OptionMarginRule(Base):
    """不可变、带版本的期权卖方保证金规则。"""

    __tablename__ = "option_margin_rule"
    __table_args__ = (
        UniqueConstraint(
            "exchange_id",
            "product_id",
            "instrument_id",
            "trading_day",
            "rule_version",
            name="uq_option_margin_rule_scope_version",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "margin_adjustment_rate >= 0 "
            "AND minimum_guarantee_rate >= 0 "
            "AND out_of_money_deduction_rate >= 0 "
            "AND minimum_underlying_margin_ratio >= 0 "
            "AND extra_margin_rate >= 0",
            name="ck_option_margin_rule_rates_nonnegative",
        ),
        Index(
            "ix_option_margin_rule_resolve",
            "exchange_id",
            "instrument_type",
            "trading_day",
            "is_active",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    exchange_id: Mapped[str] = mapped_column(String(32), nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument.id", ondelete="RESTRICT"),
        nullable=True,
    )
    instrument_type: Mapped[str] = mapped_column(String(32), nullable=False)
    margin_algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    margin_adjustment_rate: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False, default=Decimal("0")
    )
    minimum_guarantee_rate: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False, default=Decimal("0")
    )
    out_of_money_deduction_rate: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False, default=Decimal("1")
    )
    minimum_underlying_margin_ratio: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False, default=Decimal("0")
    )
    extra_margin_rate: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False, default=Decimal("0")
    )
    trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    data_source: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
