from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    JSON,
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
            "settlement_locked_volume >= 0",
            name="ck_position_settlement_locked_nonnegative",
        ),
        CheckConstraint(
            "available_volume >= 0", name="ck_position_available_nonnegative"
        ),
        CheckConstraint(
            "total_volume = today_volume + yesterday_volume",
            name="ck_position_day_volume_balance",
        ),
        CheckConstraint(
            "available_volume = total_volume - frozen_volume - settlement_locked_volume",
            name="ck_position_available_balance",
        ),
        CheckConstraint(
            "frozen_volume + settlement_locked_volume <= total_volume",
            name="ck_position_reserved_volume_within_total",
        ),
        CheckConstraint(
            "initial_occupied_margin >= 0",
            name="ck_position_initial_margin_nonnegative",
        ),
        CheckConstraint(
            "realtime_required_margin >= 0",
            name="ck_position_realtime_margin_nonnegative",
        ),
        CheckConstraint(
            "option_market_value >= 0",
            name="ck_position_option_market_value_nonnegative",
        ),
        CheckConstraint(
            "market_value >= 0",
            name="ck_position_market_value_nonnegative",
        ),
        CheckConstraint(
            "daily_pnl_base_cost >= 0",
            name="ck_position_daily_pnl_base_cost_nonnegative",
        ),
        CheckConstraint(
            "yesterday_pnl_base_cost >= 0 AND today_pnl_base_cost >= 0",
            name="ck_position_daily_pnl_bucket_cost_nonnegative",
        ),
        CheckConstraint(
            "mark_price IS NULL OR mark_price > 0",
            name="ck_position_mark_price_positive",
        ),
        CheckConstraint(
            "(mark_price IS NULL AND mark_time IS NULL AND mark_source_event_id IS NULL) "
            "OR (mark_price IS NOT NULL AND mark_time IS NOT NULL AND mark_source_event_id IS NOT NULL)",
            name="ck_position_mark_fields_consistent",
        ),
        CheckConstraint(
            "multiplier_snapshot > 0",
            name="ck_position_multiplier_positive",
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

    # 精确合约类型，避免参考数据变化改变历史持仓估值方式。
    instrument_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="FUTURES",
        index=True,
    )

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

    # 股票当日买入且尚未完成 T+1 结转的数量；期货和期权始终保持为 0。
    settlement_locked_volume: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    # 可用于卖出或平仓的数量，扣除委托冻结量和未来 T+1 交收锁定量。
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

    # 持仓建立时累计分配的原始保证金审计值；平仓不修改该历史总额。
    initial_occupied_margin: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )

    # 当前行情口径下的期权空头风险保证金。期货和期权多头保持0。
    realtime_required_margin: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )
    # PostgreSQL中保存的最近一次可靠期权标记市值。平仓事务按原持仓快照
    # 比例扣减，而不能用本次成交额替代标记市值。
    option_market_value: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )
    # 现金证券单独保存最近一次已持久化的盯市结果，不与期权市值字段混用。
    # 它们是估值事实，只用于展示、风控与日终基准，不能作为撮合输入。
    market_value: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 6), nullable=True)
    mark_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mark_source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 现金证券当日持仓盈亏 = 最新市值 - 此基准。日终将其滚动为下一日基准，
    # 卖出时按剩余持仓比例同步调整。
    daily_pnl_base_cost: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    # Cash securities have T+1 / same-day buckets.  Their daily PnL basis
    # must be reduced from the bucket actually sold, never proportionally from
    # the aggregate position.
    yesterday_pnl_base_cost: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    today_pnl_base_cost: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    daily_pnl_base_established: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    margin_rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    margin_rule_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # 卖方开仓时使用的不可变保证金规则快照。实时估值直接读取该快照，
    # 不在每个 Tick 到来时回查规则表，历史持仓也不会被新规则改写。
    margin_rule_snapshot: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )
    margin_price_mode: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    margin_underlying_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6), nullable=True
    )
    margin_option_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6), nullable=True
    )
    margin_calculated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    multiplier_snapshot: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False
    )

    # 基于原始开仓价累计确认的已实现盈亏
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )

    # 基于原始开仓价计算的累计浮动盈亏
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )

    # 当前未平仓明细相对 pnl_base_price 的当日持仓盈亏
    daily_position_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )

    # 当前交易日平仓成交相对 pnl_base_price 的累计盈亏
    daily_close_pnl: Mapped[Decimal] = mapped_column(
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
