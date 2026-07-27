from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class PositionFreezeAllocation(Base):
    """
    平仓订单与逐笔持仓明细之间的冻结分配记录。

    每条记录明确一张平仓订单从哪一笔开仓明细冻结了多少数量。部分成交
    消费consumed_volume，撤单释放released_volume，不能影响其他订单。
    """

    __tablename__ = "position_freeze_allocation"
    __table_args__ = (
        UniqueConstraint(
            "allocation_id",
            name="uq_position_freeze_allocation_id",
        ),
        UniqueConstraint(
            "order_id",
            "position_detail_id",
            name="uq_position_freeze_order_detail",
        ),
        CheckConstraint(
            "original_frozen_volume > 0",
            name="ck_position_freeze_original_positive",
        ),
        CheckConstraint(
            "remaining_frozen_volume >= 0",
            name="ck_position_freeze_remaining_nonnegative",
        ),
        CheckConstraint(
            "consumed_volume >= 0",
            name="ck_position_freeze_consumed_nonnegative",
        ),
        CheckConstraint(
            "released_volume >= 0",
            name="ck_position_freeze_released_nonnegative",
        ),
        CheckConstraint(
            "original_frozen_volume = remaining_frozen_volume "
            "+ consumed_volume + released_volume",
            name="ck_position_freeze_volume_balance",
        ),
        CheckConstraint(
            "resolved_offset_flag IN ('CLOSE_TODAY', 'CLOSE_YESTERDAY')",
            name="ck_position_freeze_resolved_offset",
        ),
        CheckConstraint(
            "commission_parameter >= 0",
            name="ck_position_freeze_commission_parameter_nonnegative",
        ),
        CheckConstraint(
            "commission_contract_multiplier > 0",
            name="ck_position_freeze_commission_multiplier_positive",
        ),
        CheckConstraint(
            "original_frozen_commission >= 0 "
            "AND remaining_frozen_commission >= 0 "
            "AND consumed_commission >= 0 "
            "AND released_commission >= 0",
            name="ck_position_freeze_commission_nonnegative",
        ),
        CheckConstraint(
            "original_frozen_commission = remaining_frozen_commission "
            "+ consumed_commission + released_commission",
            name="ck_position_freeze_commission_balance",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    allocation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    position_detail_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    exchange_id: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    offset_flag: Mapped[str] = mapped_column(String(32), nullable=False)
    # 普通 CLOSE 在下单分配时解析为明确的平今或平昨，成交和撤单均只
    # 使用该固定结果，不再根据后来变化的持仓日期重新判断。
    resolved_offset_flag: Mapped[str] = mapped_column(String(32), nullable=False)
    # 每条分配独立保存手续费规则快照，支持同一普通 CLOSE 同时包含
    # 平昨和平今，并确保规则表更新后仍能解释冻结和实际成交手续费。
    commission_type: Mapped[str] = mapped_column(String(32), nullable=False)
    commission_parameter: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False
    )
    commission_contract_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False
    )
    original_frozen_volume: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_frozen_volume: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    released_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 下列四个字段记录“预计冻结手续费”的资源流转，不等同于 Trade 中
    # 按实际成交价计算的实际手续费。
    original_frozen_commission: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False
    )
    remaining_frozen_commission: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False
    )
    consumed_commission: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )
    released_commission: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
