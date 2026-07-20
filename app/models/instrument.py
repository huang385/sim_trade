from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
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

    # 市场类型，第一版固定为 FUTURES
    market_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="FUTURES",
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

    # 是否允许交易
    is_active: Mapped[bool] = mapped_column(
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