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


class FeeRule(Base):
    """
    当前交易日手续费规则表。

    该表只保存当前交易日正在使用的手续费参数。

    commission_type 的含义：

    BY_VOLUME：
        按成交手数计算手续费。

        手续费 =
        成交手数 × 手续费参数

    BY_AMOUNT：
        按成交金额比例计算手续费。

        手续费 =
        成交价格 × 成交手数 × 合约乘数 × 手续费率
    """

    __tablename__ = "fee_rule"

    __table_args__ = (
        UniqueConstraint(
            "exchange_id",
            "symbol",
            name="uq_fee_rule_exchange_symbol",
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

    # 当前手续费规则所属交易日
    trading_day: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )

    # 手续费计算类型：
    # BY_VOLUME 按手数
    # BY_AMOUNT 按成交金额比例
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
    #
    # 外部数据源没有提供时可以为空。
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

    # 外部数据同步时间
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