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


class Position(Base):
    """
    账户按合约和多空方向汇总的期货持仓。

    同一账户、交易所、合约和持仓方向只保留一条汇总记录。
    每次开仓成交都会增加数量、持仓成本和占用保证金，并重新计算
    加权平均开仓价。逐笔来源则由PositionDetail单独保存。
    """

    __tablename__ = "position"
    __table_args__ = (
        UniqueConstraint("position_id", name="uq_position_position_id"),
        UniqueConstraint(
            "account_id",
            "exchange_id",
            "symbol",
            "direction",
            name="uq_position_account_contract_direction",
        ),
        CheckConstraint("total_volume >= 0", name="ck_position_total_nonnegative"),
        CheckConstraint("today_volume >= 0", name="ck_position_today_nonnegative"),
        CheckConstraint(
            "yesterday_volume >= 0", name="ck_position_yesterday_nonnegative"
        ),
        CheckConstraint("frozen_volume >= 0", name="ck_position_frozen_nonnegative"),
        CheckConstraint(
            "available_volume >= 0", name="ck_position_available_nonnegative"
        ),
        CheckConstraint(
            "total_volume = today_volume + yesterday_volume",
            name="ck_position_day_volume_balance",
        ),
        CheckConstraint(
            "available_volume = total_volume - frozen_volume",
            name="ck_position_available_balance",
        ),
        Index("ix_position_account_id", "account_id"),
        Index("ix_position_exchange_symbol", "exchange_id", "symbol"),
    )

    # 数据库内部自增主键
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 系统生成的全局持仓编号，供逐笔明细引用
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 持仓所属交易账户
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 行情和参考数据使用的标准合约编号
    order_book_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 交易所代码
    exchange_id: Mapped[str] = mapped_column(String(32), nullable=False)

    # 系统内部合约代码
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)

    # 持仓方向：BUY+OPEN形成LONG，SELL+OPEN形成SHORT
    direction: Mapped[str] = mapped_column(String(16), nullable=False)

    # 当前总持仓量，必须等于今仓量加昨仓量
    total_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 当前交易日开仓形成的持仓数量
    today_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 前一交易日结转的持仓数量；本阶段暂不执行每日结转
    yesterday_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 被尚未完成的平仓订单冻结的持仓数量
    frozen_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 可用于平仓的数量，必须等于总持仓量减冻结持仓量
    available_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 按成交数量加权计算的平均开仓价格
    average_open_price: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )

    # 累计持仓成本，即各笔开仓成交额之和
    position_cost: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )

    # 当前持仓实际占用的保证金
    used_margin: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )

    # 当前持仓方向累计确认的已实现盈亏
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )

    # 未实现盈亏；本阶段尚未接入盯市计算
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )

    # 持仓当前所属交易日
    trading_day: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # 首次创建持仓的时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    # 最近一次成交更新持仓的时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
