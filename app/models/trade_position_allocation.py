from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class TradePositionAllocation(Base):
    """
    一笔平仓 Trade 对具体开仓 PositionDetail 的消费明细。

    PositionFreezeAllocation 描述整张平仓订单累计冻结、成交和释放了哪些
    持仓；本表进一步固定“某一笔 Trade”实际关闭的明细、数量、保证金、
    手续费和已实现盈亏，支持部分成交、跨今昨仓和审计追踪。
    """

    __tablename__ = "trade_position_allocation"
    __table_args__ = (
        UniqueConstraint(
            "trade_position_allocation_id",
            name="uq_trade_position_allocation_id",
        ),
        UniqueConstraint(
            "trade_id",
            "allocation_id",
            name="uq_trade_position_trade_allocation",
        ),
        CheckConstraint(
            "resolved_offset_flag IN ('CLOSE_TODAY', 'CLOSE_YESTERDAY')",
            name="ck_trade_position_resolved_offset",
        ),
        CheckConstraint(
            "close_volume > 0",
            name="ck_trade_position_close_volume_positive",
        ),
        CheckConstraint(
            "released_margin >= 0",
            name="ck_trade_position_margin_nonnegative",
        ),
        CheckConstraint(
            "commission >= 0",
            name="ck_trade_position_commission_nonnegative",
        ),
        Index(
            "ix_trade_position_order_id",
            "order_id",
        ),
        Index(
            "ix_trade_position_position_detail_id",
            "position_detail_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trade_position_allocation_id: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    trade_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    allocation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    position_detail_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    order_book_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange_id: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    resolved_offset_flag: Mapped[str] = mapped_column(String(32), nullable=False)
    open_trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    close_trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    open_price: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    close_price: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    close_volume: Mapped[int] = mapped_column(Integer, nullable=False)
    released_margin: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False
    )
    commission: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    # 基于原始开仓价的累计口径已实现盈亏
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    # 基于该持仓明细 pnl_base_price 的当日平仓盈亏
    daily_close_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
