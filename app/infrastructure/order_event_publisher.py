import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from redis import Redis

from app.infrastructure.redis_keys import ORDER_EVENT_STREAM
from app.models.outbox_event import OutboxEvent


def event_json_default(value: Any) -> str:
    """
    为事件 JSON 提供安全的补充序列化规则。

    正常的订单事件在入库前已经完成转换；这里仍保留防御性处理，确保
    Decimal 永远不会经过 float，日期使用 ISO 格式，枚举使用 value。
    """

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError(f"不支持的事件字段类型: {type(value).__name__}")


class OrderEventPublisher:
    """
    把 PostgreSQL Outbox 事件发布到 Redis Stream。

    发布器只负责 XADD，不查询订单表、不修改账户，也不更新 Outbox 状态；
    状态变更由 Worker 在数据库事务中完成。
    """

    def __init__(
        self,
        redis_client: Redis,
        *,
        stream_name: str = ORDER_EVENT_STREAM,
    ):
        self.redis_client = redis_client
        self.stream_name = stream_name

    def publish(self, event: OutboxEvent) -> str:
        """发布单个事件并返回 Redis 生成的消息编号。"""

        payload_json = json.dumps(
            event.payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=event_json_default,
        )
        message_id = self.redis_client.xadd(
            self.stream_name,
            fields={
                "event_id": event.event_id,
                "event_type": event.event_type,
                "payload": payload_json,
            },
        )
        # decode_responses=False 时 redis-py 返回 bytes；统一为字符串方便日志和测试。
        if isinstance(message_id, bytes):
            return message_id.decode("utf-8")
        return str(message_id)
