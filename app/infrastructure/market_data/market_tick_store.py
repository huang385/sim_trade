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
    processed_market_tick_key,
)
from app.schemas.market_tick_schema import MarketTick


class MarketTickStoreResult(str, Enum):
    PUBLISHED = "PUBLISHED"
    DUPLICATE = "DUPLICATE"
    STALE = "STALE"


# Redis 5兼容Lua：最新行情、Stream事件和幂等标记在一个原子操作中完成。
PUBLISH_MARKET_TICK_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 1 then
    return 'DUPLICATE'
end

local current_time = redis.call('HGET', KEYS[1], 'event_time')
local current_sequence = redis.call('HGET', KEYS[1], 'sequence_id')
if current_time then
    if ARGV[2] < current_time then
        return 'STALE'
    end
    if ARGV[2] == current_time then
        local new_sequence = tonumber(ARGV[3])
        local old_sequence = tonumber(current_sequence or '-1')
        if new_sequence < old_sequence then
            return 'STALE'
        end
        if new_sequence == old_sequence then
            return 'DUPLICATE'
        end
    end
end

redis.call('HSET', KEYS[1], unpack(ARGV, 10))
redis.call(
    'XADD', KEYS[3], '*',
    'event_id', ARGV[4],
    'event_type', ARGV[5],
    'exchange_id', ARGV[6],
    'symbol', ARGV[7],
    'order_book_id', ARGV[8],
    'payload', ARGV[9]
)
redis.call('SET', KEYS[2], '1', 'EX', ARGV[1])
return 'PUBLISHED'
"""


def serialize_market_value(value: Any) -> str:
    """Redis Hash统一使用稳定字符串，Decimal绝不经过float。"""

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
    """Stream payload中价格金额为字符串，时间为ISO 8601。"""

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
    """只负责Redis最新行情、行情Stream和行情源状态读写。"""

    EVENT_TYPE = "MARKET_TICK"

    def __init__(
        self,
        redis_client: Redis,
        *,
        stream_name: str = MARKET_TICK_STREAM,
        processed_ttl_seconds: int = 86_400,
    ):
        self.redis_client = redis_client
        self.stream_name = stream_name
        self.processed_ttl_seconds = processed_ttl_seconds

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
        """原子判断新旧并发布，返回PUBLISHED、DUPLICATE或STALE。"""

        mapping = self.tick_to_mapping(tick)
        hash_arguments: list[str] = []
        for field_name, value in mapping.items():
            hash_arguments.extend((field_name, value))
        result = self.redis_client.eval(
            PUBLISH_MARKET_TICK_SCRIPT,
            3,
            market_latest_key(tick.exchange_id, tick.symbol),
            processed_market_tick_key(tick.source_event_id),
            self.stream_name,
            self.processed_ttl_seconds,
            tick.event_time.isoformat(timespec="microseconds"),
            tick.sequence_id,
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
        """幂等更新行情源运行状态和累计计数，不包含凭证。"""

        mapping = {
            key: serialize_market_value(value)
            for key, value in values.items()
        }
        if mapping:
            self.redis_client.hset(YML_FEEDHUB_STATUS_KEY, mapping=mapping)
