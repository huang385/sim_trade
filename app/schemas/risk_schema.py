from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class RiskEventResponse(BaseModel):
    """账户风险审计事件响应。金额保持Decimal，序列化后为精确十进制值。"""

    model_config = ConfigDict(from_attributes=True)
    event_id: str
    account_id: str
    event_type: str
    previous_state: str | None
    risk_state: str
    trigger_reason: str
    snapshot: dict
    business_version: int
    created_at: datetime


class LiquidationTaskResponse(BaseModel):
    """强平任务及恢复进度响应。"""

    model_config = ConfigDict(from_attributes=True)
    task_id: str
    account_id: str
    trigger_reason: str
    trigger_snapshot: dict
    status: str
    version: int
    retry_count: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None
    last_order_id: str | None
    pending_client_order_id: str | None


class RiskSnapshotResponse(BaseModel):
    account_id: str
    risk_state: str
    risk_version: int
    risk_ratio: Decimal
    equity: Decimal
    available_cash: Decimal
    risk_available_cash: Decimal
    latest_task: LiquidationTaskResponse | None = None
