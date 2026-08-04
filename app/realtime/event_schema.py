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
    occurred_at: datetime
    version: str
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
