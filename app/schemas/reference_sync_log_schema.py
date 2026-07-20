from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReferenceSyncLogCreate(BaseModel):
    """
    创建同步任务日志。
    """

    # 同步批次编号
    sync_batch_id: str = Field(min_length=1, max_length=64)

    # 数据来源
    data_source: str = "RQDATA"

    # 同步类型
    sync_type: Literal[
        "INSTRUMENT",
        "TRADING_PARAMETER",
        "FULL",
        "BACKFILL",
    ]

    # 目标交易日；合约同步时可以为空
    target_trading_day: date | None = None


class ReferenceSyncLogResponse(BaseModel):
    """
    同步任务日志返回结构。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    sync_batch_id: str
    data_source: str
    sync_type: str
    target_trading_day: date | None

    status: str

    requested_count: int
    success_count: int
    failed_count: int

    started_at: datetime
    finished_at: datetime | None
    error_message: str | None

    created_at: datetime
    updated_at: datetime