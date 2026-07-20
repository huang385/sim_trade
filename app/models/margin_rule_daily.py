from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def utc_now() -> datetime:
    """返回带时区的 UTC 时间。"""
    return datetime.now(timezone.utc)


class MarginRuleDaily(Base):
    """
    逐交易日保证金规则表。

    该表用于保存：
    1. 历史交易日保证金规则；
    2. 已经提前同步的下一个交易日保证金规则；
    3. 当前规则表的数据恢复来源；
    4. 历史成交和账户对账依据。

    同一合约、同一交易日只能保存一条保证金记录。
    """

    __tablename__ = "margin_rule_daily"

    __table_args__ = (
        UniqueConstraint(
            "exchange_id",
            "symbol",
            "trading_day",
            name="uq_margin_rule_daily_exchange_symbol_day",
        ),
    )

    # 数据库内部主键
    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    # RQData标准合约代码
    order_book_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    # 系统内部合约代码
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

    # 规则所属交易日
    trading_day: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )

    # 多头保证金率
    long_margin_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8),
        nullable=False,
    )

    # 空头保证金率
    short_margin_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8),
        nullable=False,
    )

    # 最低保证金率
    min_margin_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8),
        nullable=True,
    )

    # 数据来源
    data_source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="RQDATA",
    )

    # 同步批次编号
    #
    # 同一次同步任务写入的数据使用相同批次号，
    # 方便排查某次同步是否完整。
    sync_batch_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )

    # 从外部数据源同步的时间
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    # 数据创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    # 数据最后更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )