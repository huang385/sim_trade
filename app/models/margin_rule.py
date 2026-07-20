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


class MarginRule(Base):
    """
    当前交易日保证金规则表。

    该表只保存当前交易日正在使用的保证金规则。

    主交易程序的 OMS、账户系统和风控系统直接查询本表，
    不需要在日常下单过程中查询历史表。

    每个交易所、每个合约只保存一条当前有效记录。
    """

    __tablename__ = "margin_rule"

    __table_args__ = (
        UniqueConstraint(
            "exchange_id",
            "symbol",
            name="uq_margin_rule_exchange_symbol",
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

    # 该保证金规则所属交易日
    trading_day: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )

    # 多头保证金率
    #
    # 例如 0.12 表示 12%。
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
    #
    # 外部数据源没有提供时可以为空。
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

    # 从外部数据源同步的时间
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    # 当前记录创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    # 当前记录最后更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )