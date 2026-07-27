from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
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
    original_frozen_volume: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_frozen_volume: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    released_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
