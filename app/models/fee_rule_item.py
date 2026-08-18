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


class FeeRuleItem(Base):
    """
    按合约类型、买卖方向和开平标志解析的不可变手续费规则明细。

    现有FeeRule继续服务普通期货；期权使用本表，避免改变历史期货规则
    以及在一张表中无限增加方向字段。
    """

    __tablename__ = "fee_rule_item"
    __table_args__ = (
        UniqueConstraint(
            "exchange_id",
            "product_id",
            "instrument_id",
            "instrument_type",
            "direction",
            "offset_flag",
            "fee_type",
            "trading_day",
            "rule_version",
            name="uq_fee_rule_item_scope_version",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "commission_parameter >= 0",
            name="ck_fee_rule_item_parameter_nonnegative",
        ),
        CheckConstraint(
            "minimum_fee >= 0",
            name="ck_fee_rule_item_minimum_fee_nonnegative",
        ),
        CheckConstraint(
            "fee_type IN ('DERIVATIVE_COMMISSION', 'BROKER_COMMISSION', "
            "'STAMP_DUTY', 'TRANSFER_FEE', 'HANDLING_FEE', 'OTHER')",
            name="ck_fee_rule_item_fee_type_valid",
        ),
        CheckConstraint(
            "aggregation_scope IN ('ORDER', 'TRADE')",
            name="ck_fee_rule_item_aggregation_scope_valid",
        ),
        CheckConstraint(
            "(instrument_type IN ('STOCK', 'CONVERTIBLE_BOND') "
            "AND offset_flag IS NULL) OR "
            "(instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND') "
            "AND offset_flag IS NOT NULL)",
            name="ck_fee_rule_item_stock_offset_semantics",
        ),
        Index(
            "ix_fee_rule_item_resolve",
            "exchange_id",
            "instrument_type",
            "direction",
            "offset_flag",
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
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    offset_flag: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fee_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="DERIVATIVE_COMMISSION"
    )
    commission_type: Mapped[str] = mapped_column(String(32), nullable=False)
    commission_parameter: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False
    )
    minimum_fee: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )
    aggregation_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="TRADE"
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
