from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def utc_now() -> datetime:
    """返回带时区的 UTC 时间。"""
    return datetime.now(timezone.utc)


class ReferenceSyncLog(Base):
    """
    交易参考数据同步日志表。

    用来记录：
    1. 同步的是合约、保证金还是手续费；
    2. 目标交易日；
    3. 同步成功和失败数量；
    4. 同步状态；
    5. 同步失败原因。

    同步状态：
    RUNNING  正在执行
    SUCCESS  全部成功
    PARTIAL  部分成功
    FAILED   同步失败
    """

    __tablename__ = "reference_sync_log"

    # 数据库内部主键
    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    # 同步批次编号
    #
    # margin_rule_daily 和 fee_rule_daily 中的
    # sync_batch_id 可以关联到这个字段。
    sync_batch_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )

    # 数据来源，例如 RQDATA
    data_source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="RQDATA",
    )

    # 同步类型：
    # INSTRUMENT          合约同步
    # TRADING_PARAMETER  保证金和手续费同步
    # FULL                全量同步
    # BACKFILL            历史数据补录
    sync_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    # 目标交易日
    #
    # 只同步合约基础资料时，可以为空。
    target_trading_day: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )

    # 同步状态
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="RUNNING",
        index=True,
    )

    # 本次准备同步的总记录数
    requested_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # 成功写入或更新的记录数
    success_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # 同步失败的记录数
    failed_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # 同步开始时间
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    # 同步完成时间
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # 同步错误信息
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # 日志创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    # 日志最后更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )