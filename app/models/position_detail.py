from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class PositionDetail(Base):
    """
    逐笔开仓持仓明细。

    每一条开仓Trade只创建一条明细，用于保留具体开仓日、开仓价、
    剩余数量和原始保证金。后续实现平今、平昨时，汇总持仓不足以判断
    应该减少哪一笔持仓，因此必须保留该逐笔层数据。
    """

    __tablename__ = "position_detail"
    __table_args__ = (
        UniqueConstraint(
            "position_detail_id", name="uq_position_detail_detail_id"
        ),
        UniqueConstraint("open_trade_id", name="uq_position_detail_open_trade"),
        CheckConstraint(
            "original_volume > 0", name="ck_position_detail_original_positive"
        ),
        CheckConstraint(
            "remaining_volume >= 0", name="ck_position_detail_remaining_nonnegative"
        ),
        CheckConstraint(
            "frozen_volume >= 0", name="ck_position_detail_frozen_nonnegative"
        ),
        CheckConstraint(
            "remaining_volume <= original_volume",
            name="ck_position_detail_remaining_limit",
        ),
        CheckConstraint(
            "frozen_volume <= remaining_volume",
            name="ck_position_detail_frozen_limit",
        ),
    )

    # 数据库内部自增主键
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 系统生成的全局持仓明细编号
    position_detail_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 对应的持仓汇总编号
    position_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # 持仓所属账户编号
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # 形成这笔持仓明细的成交编号；唯一约束防止重复创建
    open_trade_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 标准合约编号
    order_book_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 交易所代码
    exchange_id: Mapped[str] = mapped_column(String(32), nullable=False)

    # 系统内部合约代码
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)

    # 持仓方向：LONG或SHORT
    direction: Mapped[str] = mapped_column(String(16), nullable=False)

    # 该笔持仓形成时的交易日
    open_trading_day: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # 该笔持仓的实际开仓成交价
    open_price: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)

    # 原始开仓数量，创建后不再修改
    original_volume: Mapped[int] = mapped_column(Integer, nullable=False)

    # 尚未被平仓的数量，本阶段等于original_volume
    remaining_volume: Mapped[int] = mapped_column(Integer, nullable=False)

    # 被平仓委托冻结的明细数量，本阶段固定为0
    frozen_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 该笔成交从订单冻结保证金中分配到的金额
    open_margin: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)

    # 该笔成交实际确认的开仓手续费
    open_commission: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)

    # 明细状态，本阶段创建时固定为OPEN
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")

    # 明细创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    # 后续平仓修改剩余数量时的更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
