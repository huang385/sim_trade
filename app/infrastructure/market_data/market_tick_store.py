import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from redis import Redis

from app.infrastructure.redis_keys import (
    MARKET_TICK_STREAM,
    YML_FEEDHUB_STATUS_KEY,
    market_latest_key,
)
from app.schemas.market_tick_schema import MarketTick


class MarketTickStoreResult(str, Enum):
    PUBLISHED = "PUBLISHED"


# Redis 5 兼容 Lua：单行情 Worker 已保证 WebSocket Tick 严格有序且不重复，
# 因此不再读取旧行情做二次判断，只保留最新 Hash 与 Stream 的原子双写。
PUBLISH_MARKET_TICK_SCRIPT = """
redis.call('HSET', KEYS[1], unpack(ARGV, 7))
redis.call(
    'XADD', KEYS[2], '*',
    'event_id', ARGV[1],
    'event_type', ARGV[2],
    'exchange_id', ARGV[3],
    'symbol', ARGV[4],
    'order_book_id', ARGV[5],
    'payload', ARGV[6]
)
return 'PUBLISHED'
"""


def serialize_market_value(value: Any) -> str:
    """Redis Hash 统一使用稳定字符串，Decimal 绝不经过 float。"""

    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def json_market_value(value: Any) -> Any:
    """Stream payload 中金额为字符串，时间使用 ISO 8601。"""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: json_market_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_market_value(item) for item in value]
    return value


class MarketTickStore:
    """只负责 Redis 最新行情、行情 Stream 和行情源状态的读写。"""

    EVENT_TYPE = "MARKET_TICK"
    OBSOLETE_SOURCE_STATUS_FIELDS = (
        "duplicate_count",
        "stale_count",
        "age_stale_count",
        "no_tick_count",
    )

    def __init__(
        self,
        redis_client: Redis,
        *,
        stream_name: str = MARKET_TICK_STREAM,
    ):
        self.redis_client = redis_client
        self.stream_name = stream_name
        self._source_status_fields_cleaned = False

    @staticmethod
    def tick_to_mapping(tick: MarketTick) -> dict[str, str]:
        return {
            field_name: serialize_market_value(value)
            for field_name, value in tick.model_dump(mode="python").items()
        }

    @staticmethod
    def tick_to_payload(tick: MarketTick) -> str:
        payload = json_market_value(tick.model_dump(mode="python"))
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def publish(self, tick: MarketTick) -> MarketTickStoreResult:
        """原子更新最新行情 Hash 并发布一条 WebSocket 行情事件。"""

        mapping = self.tick_to_mapping(tick)
        hash_arguments: list[str] = []
        for field_name, value in mapping.items():
            hash_arguments.extend((field_name, value))
        result = self.redis_client.eval(
            PUBLISH_MARKET_TICK_SCRIPT,
            2,
            market_latest_key(tick.exchange_id, tick.symbol),
            self.stream_name,
            tick.source_event_id,
            self.EVENT_TYPE,
            tick.exchange_id,
            tick.symbol,
            tick.order_book_id,
            self.tick_to_payload(tick),
            *hash_arguments,
        )
        if isinstance(result, bytes):
            result = result.decode("utf-8")
        return MarketTickStoreResult(str(result))

    def get_latest(self, exchange_id: str, symbol: str) -> dict[str, str]:
        return self.redis_client.hgetall(market_latest_key(exchange_id, symbol))

    def update_source_status(self, values: dict[str, Any]) -> None:
        """幂等更新行情源状态和累计计数，调用方不得传入敏感凭证。"""

        mapping = {key: serialize_market_value(value) for key, value in values.items()}
        if mapping:
            if not self._source_status_fields_cleaned:
                # 旧版本累计字段不会被 HSET 自动删除；新 Worker 首次写状态时
                # 清理一次，避免运维人员误以为新逻辑仍在执行新旧行情过滤。
                self.redis_client.hdel(
                    YML_FEEDHUB_STATUS_KEY,
                    *self.OBSOLETE_SOURCE_STATUS_FIELDS,
                )
            self.redis_client.hset(YML_FEEDHUB_STATUS_KEY, mapping=mapping)
            self._source_status_fields_cleaned = True
