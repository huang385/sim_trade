from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
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


class Order(Base):
    """
    订单主表。

    记录已经完成校验和资源冻结的限价开仓、平仓订单，并承载后续部分成交、
    完全成交和主动撤单状态。成交、资金释放和持仓变化由对应事务服务负责。

    表名使用 orders，避免直接使用 SQL 保留字 order。

    核心一致性原则：
    1. 账户资金冻结和订单写入必须在同一个数据库事务中完成；
    2. order_id 是系统生成的全局订单编号；
    3. account_id + client_order_id 保证客户端请求幂等；
    4. 未成交订单只记录冻结金额，不修改实际占用保证金和手续费。
    """

    __tablename__ = "orders"

    __table_args__ = (
        # 委托总量必须始终等于成交量、剩余量和撤销量之和。
        CheckConstraint(
            "total_volume = traded_volume + remaining_volume + cancelled_volume",
            name="ck_order_volume_balance",
        ),
        # 系统订单编号在数据库层保持全局唯一。
        UniqueConstraint(
            "order_id",
            name="uq_order_order_id",
        ),
        # 同一账户不能重复使用同一个客户端订单编号。
        # 这是数据库层最后一道幂等保护。
        UniqueConstraint(
            "account_id",
            "client_order_id",
            name="uq_order_account_client_order_id",
        ),
        # 撮合服务后续会按交易所和合约扫描订单，提前建立组合索引。
        Index(
            "ix_order_exchange_symbol",
            "exchange_id",
            "symbol",
        ),
        Index(
            "ix_order_created_at",
            "created_at",
        ),
        CheckConstraint(
            "commission_parameter >= 0",
            name="ck_order_commission_parameter_nonnegative",
        ),
        CheckConstraint(
            "commission_contract_multiplier > 0",
            name="ck_order_commission_multiplier_positive",
        ),
        CheckConstraint(
            "frozen_cash >= 0",
            name="ck_order_frozen_cash_nonnegative",
        ),
        CheckConstraint(
            "margin_underlying_price IS NULL OR margin_underlying_price >= 0",
            name="ck_order_margin_underlying_price_nonnegative",
        ),
        CheckConstraint(
            "margin_option_price IS NULL OR margin_option_price >= 0",
            name="ck_order_margin_option_price_nonnegative",
        ),
    )

    # 数据库内部自增主键
    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )
    # 系统订单编号，对外查询订单时使用
    order_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    # 客户端订单编号，用于识别重试和重复提交
    client_order_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    # 下单账户编号
    account_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    # 外部参考数据使用的标准合约编号
    order_book_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    # 系统内部使用的合约代码
    symbol: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    # 交易所代码
    exchange_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
    )
    # 订单所属交易日；参考数据规则的一致性由独立同步项目负责
    trading_day: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )

    # 接单时保存的精确合约类型。后续参考数据变化不能改变历史订单的
    # 结算分派方式。
    instrument_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="FUTURES",
        index=True,
    )
    # 期权活动订单保存标的合约定位字段，使行情Worker无需每500ms回查
    # Instrument关系即可找到需要重估保证金的卖出开仓订单。
    underlying_order_book_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    underlying_exchange_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    underlying_symbol: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    # 买卖方向：BUY 或 SELL
    direction: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )
    # 开平标志：OPEN、CLOSE、CLOSE_TODAY 或 CLOSE_YESTERDAY
    offset_flag: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    # 订单类型；当前开平仓链路支持 LIMIT
    order_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )

    # 手续费规则快照。预计冻结使用限价，实际成交使用成交价，但二者必须
    # 使用订单接受时固定下来的计算方式、参数和合约乘数，不能重新读取
    # 可能已经更新的当前手续费规则。
    commission_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    commission_parameter: Mapped[Decimal] = mapped_column(
        Numeric(24, 12),
        nullable=False,
    )
    commission_contract_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
    )
    fee_rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fee_rule_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    fee_rule_snapshot: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )

    # 限价单委托价格
    limit_price: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
    )
    # 委托总数量
    total_volume: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    # 已成交数量；由撮合结算按每次实际成交量累计
    traded_volume: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    # 剩余未成交数量；成交时递减，主动撤销剩余数量后归零
    remaining_volume: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    # 已撤销数量；主动撤单时只增加当前剩余未成交数量。
    cancelled_volume: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    # 按成交数量加权计算的平均成交价格；尚未成交时为空
    average_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6),
        nullable=True,
        default=None,
    )

    # 订单业务状态
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
    )
    # 订单提交处理状态
    submit_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    # 本订单预计并已冻结的开仓保证金
    frozen_margin: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )
    # 期权买入开仓或买入平空订单尚未成交部分冻结的最大权利金。
    frozen_cash: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )
    # 本订单预计并已冻结的手续费
    frozen_commission: Mapped[Decimal] = mapped_column(
        Numeric(24, 6),
        nullable=False,
        default=Decimal("0"),
    )
    # 平仓订单尚未成交部分冻结的持仓数量；开仓订单始终为0
    frozen_position_volume: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # 期权卖方保证金计算审计快照。Decimal在JSON中必须保存为字符串，
    # 使历史活动订单不受当前规则版本变化影响。
    margin_rule_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    margin_rule_version: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    margin_price_mode: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )
    margin_underlying_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6),
        nullable=True,
    )
    margin_option_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6),
        nullable=True,
    )
    margin_rule_snapshot: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
    )
    margin_snapshot_schema_version: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )
    margin_calculation_version: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    # 拒绝原因代码；ACCEPTED订单为空
    reject_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    # 拒绝原因说明；ACCEPTED订单为空
    reject_message: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
    )

    # 订单创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    # 订单完成校验和冻结、正式被接受的时间
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 用户主动撤销订单的时间；重复撤单始终保留第一次时间。
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 订单最后更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
