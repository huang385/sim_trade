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


class FeeRuleDaily(Base):
    """
    逐交易日手续费规则表。

    该表用于保存：
    1. 历史交易日手续费规则；
    2. 提前同步的下一交易日手续费规则；
    3. 当前手续费表的数据恢复来源；
    4. 历史成交手续费对账依据。
    """

    __tablename__ = "fee_rule_daily"

    __table_args__ = (
        UniqueConstraint(
            "exchange_id",
            "symbol",
            "trading_day",
            name="uq_fee_rule_daily_exchange_symbol_day",
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

    # 手续费计算方式
    commission_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    # 开仓手续费参数
    open_commission: Mapped[Decimal] = mapped_column(
        Numeric(24, 12),
        nullable=False,
        default=Decimal("0"),
    )

    # 普通平仓手续费参数
    close_commission: Mapped[Decimal] = mapped_column(
        Numeric(24, 12),
        nullable=False,
        default=Decimal("0"),
    )

    # 平今仓手续费参数
    close_today_commission: Mapped[Decimal] = mapped_column(
        Numeric(24, 12),
        nullable=False,
        default=Decimal("0"),
    )

    # 平今折扣率
    discount_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 12),
        nullable=True,
    )

    # 数据来源
    data_source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="RQDATA",
    )

    # 同步批次编号
    sync_batch_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )

    # 外部数据同步时间
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