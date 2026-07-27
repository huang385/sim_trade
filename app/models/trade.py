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


class Trade(Base):
    """
    成交记录表。

    一条记录表示某一笔订单被一条真实 WebSocket Tick 撮合出的结果。
    ``order_id + market_event_id`` 是消费重复投递时的最终幂等防线。

    核心原则：
    1. 一条行情对同一订单最多产生一条成交；
    2. 成交时间使用行情源事件时间，不能使用 Worker 当前处理时间；
    3. margin 和 commission 记录本次从订单冻结资源中转出的金额；
    4. 所有价格和金额均使用 Decimal 对应的 Numeric，禁止 float。
    """

    __tablename__ = "trade"
    __table_args__ = (
        UniqueConstraint("trade_id", name="uq_trade_trade_id"),
        UniqueConstraint(
            "order_id",
            "market_event_id",
            name="uq_trade_order_market_event",
        ),
        CheckConstraint("trade_price > 0", name="ck_trade_price_positive"),
        CheckConstraint("trade_volume > 0", name="ck_trade_volume_positive"),
        CheckConstraint("turnover >= 0", name="ck_trade_turnover_nonnegative"),
        CheckConstraint("margin >= 0", name="ck_trade_margin_nonnegative"),
        CheckConstraint(
            "commission >= 0",
            name="ck_trade_commission_nonnegative",
        ),
        Index("ix_trade_exchange_symbol", "exchange_id", "symbol"),
    )

    # 数据库内部自增主键，只用于表内排序和关联查询
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 系统生成的全局成交编号，对外查询成交时使用
    trade_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 产生成交的系统订单编号
    order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # 成交所属模拟交易账户编号
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # 行情源事件编号。它与order_id组成成交幂等唯一键。
    market_event_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Redis Stream消息编号，用于定位Pending、重试和死信中的原始消息。
    market_stream_message_id: Mapped[str] = mapped_column(
        String(64), nullable=False
    )

    # 行情和参考数据使用的标准合约编号
    order_book_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 交易所代码，例如SHFE、DCE
    exchange_id: Mapped[str] = mapped_column(String(32), nullable=False)

    # 系统内部合约代码，例如AG2609
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)

    # 成交所属交易日，沿用订单交易日
    trading_day: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # 原订单买卖方向：BUY或SELL
    direction: Mapped[str] = mapped_column(String(16), nullable=False)

    # 原订单开平标志：OPEN、CLOSE、CLOSE_TODAY或CLOSE_YESTERDAY
    offset_flag: Mapped[str] = mapped_column(String(32), nullable=False)

    # 实际成交价格：买入使用卖一价，卖出使用买一价
    trade_price: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)

    # 本次成交手数，必须大于0
    trade_volume: Mapped[int] = mapped_column(Integer, nullable=False)

    # 成交金额 = 成交价 × 成交量 × 合约乘数
    turnover: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)

    # OPEN表示新增占用保证金，CLOSE类成交表示本次释放的保证金
    margin: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)

    # 本次从订单冻结手续费转为账户实际手续费的金额
    commission: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)

    # OPEN成交为0；CLOSE类成交记录本次逐笔计算的已实现盈亏
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(24, 6), nullable=False, default=Decimal("0")
    )

    # 成交时间使用触发撮合的 Tick.event_time，而不是 Worker 的处理时间。
    trade_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # PostgreSQL实际写入成交记录的时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
