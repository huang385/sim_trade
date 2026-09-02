import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable

from redis import Redis

from app.infrastructure.redis_keys import (
    FUTURES_MARKET_SOURCE_STATUS_KEY,
    FUTURES_MARKET_TICK_STREAM,
    market_latest_key,
)
from app.schemas.market_tick_schema import MarketTick


class MarketTickStoreResult(str, Enum):
    PUBLISHED = "PUBLISHED"
    IGNORED_STALE = "IGNORED_STALE"


# Redis 5 兼容 Lua：单行情 Worker 已保证 WebSocket Tick 严格有序且不重复，
# 因此不再读取旧行情做二次判断，只保留最新 Hash 与 Stream 的原子双写。
PUBLISH_MARKET_TICK_SCRIPT = """
-- REST 快照与订阅回调可能并发到达。快照不能覆盖已经更晚的实时
-- Tick；同一事件时间也优先保留订阅回调。
local incoming_ingest_type = ''
local incoming_time = ''
for index = 8, #ARGV, 2 do
    if ARGV[index] == 'ingest_type' then
        incoming_ingest_type = ARGV[index + 1]
    elseif ARGV[index] == 'event_time' then
        incoming_time = ARGV[index + 1]
    end
end
if incoming_ingest_type == 'REST_SNAPSHOT' then
    local current_time = redis.call('HGET', KEYS[1], 'event_time')
    if current_time and current_time ~= '' and current_time >= incoming_time then
        return 'IGNORED_STALE'
    end
end
local stream_message_id = redis.call(
    'XADD', KEYS[2], '*',
    'event_id', ARGV[1],
    'event_type', ARGV[2],
    'exchange_id', ARGV[3],
    'symbol', ARGV[4],
    'order_book_id', ARGV[5],
    'payload', ARGV[6]
)
redis.call(
    'HSET', KEYS[1],
    'stream_message_id', stream_message_id,
    unpack(ARGV, 8)
)
if ARGV[7] ~= '' then
    redis.call(
        'HSET', KEYS[1],
        'subscription_generation', ARGV[7]
    )
else
    redis.call('HDEL', KEYS[1], 'subscription_generation')
end
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
        stream_name: str = FUTURES_MARKET_TICK_STREAM,
        source_status_key: str = FUTURES_MARKET_SOURCE_STATUS_KEY,
    ):
        self.redis_client = redis_client
        self.stream_name = stream_name
        self.source_status_key = source_status_key
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

    @staticmethod
    def mapping_to_tick(values: dict[str, str]) -> MarketTick:
        """
        把Redis Hash还原为MarketTick。

        Redis Hash不能保存None，写入时统一序列化为空字符串；恢复Pydantic
        模型前必须把这些空字符串还原为None，否则可选Decimal、时间和整数
        字段会被误判为格式错误。
        """

        return MarketTick.model_validate(
            {
                key: None if value == "" else value
                for key, value in values.items()
            }
        )

    def publish(
        self,
        tick: MarketTick,
        *,
        subscription_generation: int | None = None,
    ) -> MarketTickStoreResult:
        """原子更新最新行情 Hash 并发布一条 WebSocket 行情事件。"""

        mapping = self.tick_to_mapping(tick)
        hash_arguments: list[str] = []
        for field_name, value in mapping.items():
            hash_arguments.extend((field_name, value))
        result = self.redis_client.eval(
            PUBLISH_MARKET_TICK_SCRIPT,
            2,
            market_latest_key(tick.exchange_id, tick.order_book_id),
            self.stream_name,
            tick.source_event_id,
            self.EVENT_TYPE,
            tick.exchange_id,
            tick.symbol,
            tick.order_book_id,
            self.tick_to_payload(tick),
            (
                str(subscription_generation)
                if subscription_generation is not None
                else ""
            ),
            *hash_arguments,
        )
        if isinstance(result, bytes):
            result = result.decode("utf-8")
        return MarketTickStoreResult(str(result))

    def get_latest(
        self,
        exchange_id: str,
        order_book_id: str,
    ) -> dict[str, str]:
        return self.redis_client.hgetall(
            market_latest_key(exchange_id, order_book_id)
        )

    def get_latest_many(
        self,
        contract_keys: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], dict[str, str]]:
        """
        使用一次非事务 Pipeline 批量读取多个合约的最新行情。

        合约键先标准化并去重，随后按稳定顺序排队，确保 Pipeline 返回值能够
        与交易所、合约一一对应。空输入直接返回，不向 Redis 发送空命令。
        """

        keys = sorted(
            {
                (
                    str(exchange_id).strip().upper(),
                    str(order_book_id).strip().upper(),
                )
                for exchange_id, order_book_id in contract_keys
            }
        )
        if not keys:
            return {}

        pipeline = self.redis_client.pipeline(transaction=False)
        for exchange_id, order_book_id in keys:
            pipeline.hgetall(market_latest_key(exchange_id, order_book_id))
        return dict(zip(keys, pipeline.execute(), strict=True))

    def update_source_status(self, values: dict[str, Any]) -> None:
        """幂等更新行情源状态和累计计数，调用方不得传入敏感凭证。"""

        mapping = {key: serialize_market_value(value) for key, value in values.items()}
        if mapping:
            if not self._source_status_fields_cleaned:
                # 旧版本累计字段不会被 HSET 自动删除；新 Worker 首次写状态时
                # 清理一次，避免运维人员误以为新逻辑仍在执行新旧行情过滤。
                self.redis_client.hdel(
                    self.source_status_key,
                    *self.OBSOLETE_SOURCE_STATUS_FIELDS,
                )
            self.redis_client.hset(
                self.source_status_key,
                mapping=mapping,
            )
            self._source_status_fields_cleaned = True
