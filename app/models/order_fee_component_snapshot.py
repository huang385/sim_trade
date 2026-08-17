from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
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


class OrderFeeComponentSnapshot(Base):
    """订单受理时固化的单项手续费规则，不随之后规则更新改变。"""

    __tablename__ = "order_fee_component_snapshot"
    __table_args__ = (
        UniqueConstraint("order_id", "fee_type", name="uq_order_fee_component_type"),
        Index("ix_order_fee_component_snapshot_order", "order_id"),
        CheckConstraint(
            "minimum_fee >= 0", name="ck_order_fee_snapshot_minimum_nonnegative"
        ),
        CheckConstraint(
            "contract_multiplier > 0", name="ck_order_fee_snapshot_multiplier_positive"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.order_id", ondelete="RESTRICT"), nullable=False
    )
    fee_type: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_item_id: Mapped[int] = mapped_column(
        ForeignKey("fee_rule_item.id", ondelete="RESTRICT"), nullable=False
    )
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    calculation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    commission_parameter: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    minimum_fee: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    aggregation_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    contract_multiplier: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    data_source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
