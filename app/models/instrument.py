from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def utc_now() -> datetime:
    """返回带时区的 UTC 时间。"""
    return datetime.now(timezone.utc)


class Instrument(Base):
    """
    合约基础信息表。

    合约数据主要由 RQData 或自有行情系统同步。

    该表用于：
    1. 判断合约是否存在；
    2. 判断合约是否允许交易；
    3. 校验下单价格是否符合最小变动价位；
    4. 校验下单数量是否合法；
    5. 计算成交金额、保证金和盈亏。
    """

    __tablename__ = "instrument"

    __table_args__ = (
        # 同一交易所下，同一合约代码只能存在一条记录
        UniqueConstraint(
            "exchange_id",
            "symbol",
            name="uq_instrument_exchange_symbol",
        ),
        CheckConstraint(
            "instrument_type NOT IN ('FUTURES_OPTION', 'INDEX_OPTION') "
            "OR underlying_instrument_id IS NOT NULL",
            name="ck_instrument_option_underlying",
        ),
        CheckConstraint(
            "instrument_type NOT IN ('FUTURES_OPTION', 'INDEX_OPTION') "
            "OR option_type IN ('CALL', 'PUT')",
            name="ck_instrument_option_type",
        ),
        CheckConstraint(
            "instrument_type NOT IN ('FUTURES_OPTION', 'INDEX_OPTION') "
            "OR strike_price > 0",
            name="ck_instrument_option_strike",
        ),
        CheckConstraint(
            "instrument_type NOT IN ('FUTURES_OPTION', 'INDEX_OPTION') "
            "OR expire_date IS NOT NULL",
            name="ck_instrument_option_expiry",
        ),
        CheckConstraint(
            "underlying_instrument_id IS NULL "
            "OR underlying_instrument_id <> id",
            name="ck_instrument_not_self_underlying",
        ),
        CheckConstraint(
            "instrument_type <> 'INDEX' OR is_tradeable = false",
            name="ck_instrument_index_not_tradeable",
        ),
        CheckConstraint(
            "instrument_type = 'INDEX' OR contract_multiplier > 0",
            name="ck_instrument_derivative_multiplier_positive",
        ),
        CheckConstraint(
            "(instrument_type <> 'STOCK' OR market_type = 'STOCK') AND "
            "(instrument_type <> 'CONVERTIBLE_BOND' OR market_type = 'BOND') AND "
            "(instrument_type <> 'ETF' OR market_type = 'FUND')",
            name="ck_instrument_stock_market_type",
        ),
        CheckConstraint(
            "instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND', 'ETF') OR contract_multiplier = 1",
            name="ck_instrument_stock_multiplier_one",
        ),
        CheckConstraint(
            "instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND', 'ETF') OR ("
            "underlying_instrument_id IS NULL AND option_type IS NULL AND "
            "strike_price IS NULL AND exercise_style IS NULL AND "
            "settlement_type IS NULL)",
            name="ck_instrument_stock_option_fields_empty",
        ),
        CheckConstraint(
            "instrument_type <> 'ETF' OR (fund_type IS NOT NULL AND "
            "market_tplus IS NOT NULL AND market_tplus IN (0, 1) AND "
            "round_lot IS NOT NULL AND round_lot > 0)",
            name="ck_instrument_etf_reference_fields",
        ),
        Index(
            "ix_instrument_underlying_type",
            "underlying_instrument_id",
            "instrument_type",
        ),
    )

    # 数据库内部主键
    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    # RQData 使用的标准合约代码，例如 RB2610
    order_book_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )

    # 系统内部合约代码，例如 RB2610
    symbol: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    # 交易所代码，例如 SHFE、DCE、CZCE、CFFEX
    exchange_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
    )

    # 合约中文名称，例如 螺纹钢2610
    instrument_name: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    # 品种代码，例如 RB、CU、IF
    product_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )

    # ETF数据源属性。交易执行仍以版本化交易规则为准；这些字段保留YMM原始
    # 事实，供规则生成、展示、审计和未来申购赎回模块使用。
    fund_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    market_tplus: Mapped[int | None] = mapped_column(Integer, nullable=True)
    round_lot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    least_redeem: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reference_underlying_order_book_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    # 市场类型，第一版固定为 FUTURES
    market_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="FUTURES",
    )

    # 精确合约类型。market_type继续保留宽泛市场分类，实际交易业务必须
    # 使用本字段区分普通期货、商品期权、指数和股指期权。
    instrument_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="FUTURES",
        index=True,
    )

    # 期权标的合约的Instrument内部主键。商品期权关联FUTURES，
    # 股指期权关联INDEX；普通期货和指数保持为空。
    underlying_instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # CALL或PUT。非期权合约保持为空。
    option_type: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )

    # 期权行权价格。所有资金计算仍使用Decimal。
    strike_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6),
        nullable=True,
    )

    # AMERICAN或EUROPEAN；本阶段只保存，不执行行权。
    exercise_style: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )

    # PHYSICAL或CASH；本阶段只保存，不执行交割或现金结算。
    settlement_type: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )

    # 合约乘数
    #
    # 例如螺纹钢合约乘数为 10。
    contract_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        nullable=False,
        default=Decimal("1"),
    )

    # 最小变动价位
    #
    # 例如 price_tick=1 时，3500.5 属于非法价格。
    price_tick: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        nullable=False,
        default=Decimal("1"),
    )

    # 单笔最小下单数量
    min_volume: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )

    # 单笔最大下单数量
    max_volume: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1_000_000,
    )

    # 合约上市日期
    listed_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    # 合约到期或退市日期
    expire_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )

    # 最后交易日只作为参考数据保存；本阶段不增加新的下单时间限制。
    last_trading_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )

    # 是否允许交易
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
    )

    # 与is_active分离的可交易标志。INDEX默认不可交易；本阶段只维护
    # 字段，不改变现有订单校验对is_active的处理方式。
    is_tradeable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
    )

    # 数据来源：
    # RQDATA   RQData同步
    # MANUAL   人工录入
    # INTERNAL 自有系统同步
    data_source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="RQDATA",
    )

    # 最近一次从外部数据源同步的时间
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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
