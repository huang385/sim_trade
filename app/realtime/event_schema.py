from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.realtime.event_enums import RealtimeEventType


class RealtimeEventEnvelope(BaseModel):
    """期货、期权和账户事件共用的不可变传输Envelope。"""

    event_id: str
    event_type: RealtimeEventType
    account_id: str | None = None
    entity_id: str | None = None
    # 产品维度是可选路由元数据。旧事件可以不携带，新事件由服务端事实填充。
    account_type: str | None = None
    instrument_type: str | None = None
    occurred_at: datetime
    # version是目标Redis Stream传输游标；business_version是业务聚合版本。
    # 两者用途不同，禁止用重试后生成的Stream编号判断订单状态先后。
    version: str
    business_version: str | None = None
    # Redis PnL单写者为账户和持仓估值生成的独立周期版本。它不属于
    # PostgreSQL Outbox业务版本域，也不能替代Stream传输游标。
    realtime_version: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        """事件时间统一为带时区时间，避免延迟统计出现朴素时间混算。"""

        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class SubscribeMessage(BaseModel):
    """客户端订阅或取消订阅账户时提交的消息。"""

    action: Literal["subscribe", "unsubscribe"]
    account_ids: list[str] = Field(min_length=1)

    @field_validator("account_ids")
    @classmethod
    def normalize_account_ids(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(value).strip() for value in values))
        if not normalized or any(not value for value in normalized):
            raise ValueError("account_ids不能为空")
        return normalized


class PongMessage(BaseModel):
    """客户端应用层心跳响应。"""

    action: Literal["pong"]
