from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def utc_now() -> datetime:
    """
    返回带时区的 UTC 时间。

    数据库存储统一时间，展示时再转换为本地时间。
    """
    return datetime.now(timezone.utc)


class Account(Base):
    """
    模拟交易账户资金表。

    该表保存账户当前资金快照。

    核心原则：
    1. 下单时只冻结资金或保证金；
    2. 订单成交后，才根据 Trade 更新实际保证金、手续费和盈亏；
    3. 行情变化只更新浮动盈亏、动态权益和风险率；
    4. 账户资金变化不能由客户端直接修改。
    """

    __tablename__ = "account"
    __table_args__ = (
        CheckConstraint(
            "option_used_margin >= 0",
            name="ck_account_option_used_margin_nonnegative",
        ),
        CheckConstraint(
            "option_realtime_required_margin >= 0",
            name="ck_account_option_realtime_margin_nonnegative",
        ),
        CheckConstraint(
            "long_option_market_value >= 0",
            name="ck_account_long_option_value_nonnegative",
        ),
        CheckConstraint(
            "short_option_market_value >= 0",
            name="ck_account_short_option_value_nonnegative",
        ),
        CheckConstraint(
            "stock_market_value >= 0",
            name="ck_account_stock_market_value_nonnegative",
        ),
    )

    # 数据库内部自增主键
    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    # 交易账户编号，例如 092001
    account_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )

    # 用户编号，一个用户可以拥有多个交易账户
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("app_user.user_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # 账户名称，例如：测试期货账户
    account_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )

    # 账户类型，第一版主要使用 FUTURES
    account_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="FUTURES",
    )

    # 账户级期权交易权限。系统总开关和产品开关同时开启时，本字段仍需
    # 为True，期权订单才能进入资金冻结流程。
    option_trading_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    # 创建账户时设置的初始资金
    initial_cash: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 当前现金余额
    #
    # 手续费扣除和平仓盈亏会影响该字段。
    cash_balance: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 当前可用于下单的资金
    available_cash: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 下单后、成交前被冻结的普通资金
    frozen_cash: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 动态权益
    #
    # 一般可以理解为：
    # 现金余额 + 浮动盈亏
    equity: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 已成交持仓实际占用的保证金
    used_margin: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 未成交开仓订单预冻结的保证金
    frozen_margin: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 基于原始开仓价累计计算的已实现盈亏
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 基于原始开仓价计算的累计浮动盈亏
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 当前未平仓持仓相对 pnl_base_price 的当日持仓盈亏
    daily_position_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # used_margin中由期权空头持仓实际占用的保证金。
    option_used_margin: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 500ms实时估值得出的期权空头风险保证金；它是派生值，不能由估值
    # Worker直接覆盖上面的实际资金占用字段。
    option_realtime_required_margin: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 期权多头、空头绝对市值以及二者净额。空头市值保存非负绝对值。
    long_option_market_value: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )
    short_option_market_value: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )
    net_option_market_value: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 股票账户未来用于保存股票持仓市值；本阶段只保存该字段，不接入估值。
    stock_market_value: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )

    # 使用实时风险保证金计算的可用资金。available_cash继续表示数据库
    # 账面冻结口径，本字段供风险限制和实时页面使用。
    risk_available_cash: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # NORMAL、MARGIN_DEFICIT或VALUATION_UNAVAILABLE。
    risk_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="NORMAL",
    )

    # 风险状态的账户内单调业务版本。每次状态转换在账户行锁内递增，
    # WebSocket客户端据此拒绝迟到的旧风险事件。
    risk_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    # 当前交易日平仓成交相对 pnl_base_price 的累计盈亏
    daily_close_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 当前交易日已经实际发生的成交手续费
    daily_commission: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 当日净盈亏 = 当日持仓盈亏 + 当日平仓盈亏 - 当日手续费
    daily_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 后端统一累计经济净盈亏，前端不得再自行拼接。
    cumulative_net_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )

    # 成交后已经实际扣除的手续费
    used_commission: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 未成交订单预冻结的手续费
    frozen_commission: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # 账户风险率
    #
    # 第一版可以使用：
    # 风险度 = 占用保证金 / 动态权益。
    risk_ratio: Mapped[Decimal] = mapped_column(
        Numeric(18, 8),
        nullable=False,
        default=Decimal("0"),
    )

    # 账户状态：
    # NORMAL      正常
    # DISABLED    禁止交易
    # LIQUIDATION 强平中
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="NORMAL",
    )

    # 当前账户所属交易日
    trading_day: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )

    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    # 最后更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
