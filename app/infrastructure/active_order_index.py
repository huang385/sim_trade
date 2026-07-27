from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from redis import Redis

from app.infrastructure.redis_keys import (
    ACTIVE_ORDERS_ALL_KEY,
    account_active_orders_key,
    active_order_key,
    instrument_active_orders_key,
    processed_order_event_key,
)


# 一个 Lua 脚本同时写详情、三个倒排集合和事件幂等标记。
# 这样即使 Consumer 在 Redis 操作中途退出，也不会留下只写了一半的索引。
ADD_ACTIVE_ORDER_SCRIPT = """
local is_new_event = 0
if redis.call('EXISTS', KEYS[5]) == 0 then
    is_new_event = 1
end
redis.call('HSET', KEYS[1], unpack(ARGV, 3))
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('SADD', KEYS[3], ARGV[1])
redis.call('SADD', KEYS[4], ARGV[1])
if is_new_event == 1 then
    redis.call('SET', KEYS[5], '1', 'EX', ARGV[2])
end
return is_new_event
"""


# 删除也通过 Lua 同时完成，避免详情和集合成员状态不一致。
REMOVE_ACTIVE_ORDER_SCRIPT = """
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[2], ARGV[1])
redis.call('SREM', KEYS[3], ARGV[1])
redis.call('SREM', KEYS[4], ARGV[1])
if KEYS[5] ~= '' then
    redis.call('SET', KEYS[5], '1', 'EX', ARGV[2])
end
return 1
"""


# 重建不属于Stream事件处理：只覆盖Hash和三个Set，不写processed事件标记。
UPSERT_ACTIVE_ORDER_FOR_REBUILD_SCRIPT = """
redis.call('HSET', KEYS[1], unpack(ARGV, 2))
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('SADD', KEYS[3], ARGV[1])
redis.call('SADD', KEYS[4], ARGV[1])
return 1
"""


ACTIVE_ORDER_FIELDS = (
    "order_id",
    "account_id",
    "exchange_id",
    "symbol",
    "order_book_id",
    "direction",
    "offset_flag",
    "order_type",
    "limit_price",
    "total_volume",
    "traded_volume",
    "remaining_volume",
    "cancelled_volume",
    "average_price",
    "frozen_margin",
    "frozen_commission",
    "frozen_position_volume",
    "status",
    "trading_day",
    "accepted_at",
    "updated_at",
)


def serialize_redis_value(value: Any) -> str:
    """
    把订单字段转换成稳定的 Redis 字符串。

    Decimal 禁止经过 float；日期和时间使用 ISO 格式；None 统一保存为空串。
    """

    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


class ActiveOrderIndex:
    """
    Redis 活动订单索引适配器。

    本类只读写 Redis，不访问 PostgreSQL，也不判断订单是否应该进入索引。
    PostgreSQL orders 表仍然是事实来源，Redis 索引可以随时从数据库重建。
    """

    def __init__(self, redis_client: Redis):
        self.redis_client = redis_client

    @staticmethod
    def order_to_mapping(order: Any) -> dict[str, str]:
        """从订单对象提取活动索引需要的字段。"""

        return {
            field: serialize_redis_value(getattr(order, field, None))
            for field in ACTIVE_ORDER_FIELDS
        }

    def add_active_order(
        self,
        order: Any,
        *,
        event_id: str,
        processed_ttl_seconds: int,
    ) -> bool:
        """
        原子注册活动订单并写入事件幂等标记。

        Hash 和 Set 每次都按 PostgreSQL 最新快照幂等覆盖；返回 True 表示
        首次看到该 event_id，False 表示重复投递。重复事件不会产生重复集合
        成员，但仍可修复陈旧或被误删的 Redis 派生索引。
        """

        mapping = self.order_to_mapping(order)
        hash_arguments: list[str] = []
        for field, value in mapping.items():
            hash_arguments.extend((field, value))
        result = self.redis_client.eval(
            ADD_ACTIVE_ORDER_SCRIPT,
            5,
            active_order_key(order.order_id),
            instrument_active_orders_key(order.exchange_id, order.symbol),
            account_active_orders_key(order.account_id),
            ACTIVE_ORDERS_ALL_KEY,
            processed_order_event_key(event_id),
            order.order_id,
            processed_ttl_seconds,
            *hash_arguments,
        )
        return bool(result)

    def upsert_active_order_for_rebuild(self, order: Any) -> None:
        """
        根据PostgreSQL最新快照原子修复活动订单索引。

        重建不是事件消费，因此不创建或刷新processed_order_event键。
        """

        mapping = self.order_to_mapping(order)
        hash_arguments: list[str] = []
        for field, value in mapping.items():
            hash_arguments.extend((field, value))
        self.redis_client.eval(
            UPSERT_ACTIVE_ORDER_FOR_REBUILD_SCRIPT,
            4,
            active_order_key(order.order_id),
            instrument_active_orders_key(order.exchange_id, order.symbol),
            account_active_orders_key(order.account_id),
            ACTIVE_ORDERS_ALL_KEY,
            order.order_id,
            *hash_arguments,
        )

    def update_active_order(
        self,
        order: Any,
        *,
        event_id: str,
        processed_ttl_seconds: int,
    ) -> bool:
        """使用数据库最新快照幂等覆盖活动订单详情。"""

        return self.add_active_order(
            order,
            event_id=event_id,
            processed_ttl_seconds=processed_ttl_seconds,
        )

    def remove_active_order(
        self,
        *,
        order_id: str,
        account_id: str,
        exchange_id: str,
        symbol: str,
        event_id: str | None = None,
        processed_ttl_seconds: int = 604800,
    ) -> None:
        """原子删除订单详情、合约、账户和全局集合成员。"""

        processed_key = (
            processed_order_event_key(event_id) if event_id else ""
        )
        self.redis_client.eval(
            REMOVE_ACTIVE_ORDER_SCRIPT,
            5,
            active_order_key(order_id),
            instrument_active_orders_key(exchange_id, symbol),
            account_active_orders_key(account_id),
            ACTIVE_ORDERS_ALL_KEY,
            processed_key,
            order_id,
            processed_ttl_seconds,
        )

    def get_active_order(self, order_id: str) -> dict[str, str]:
        """读取单笔活动订单详情；不存在时返回空字典。"""

        return self.redis_client.hgetall(active_order_key(order_id))

    def list_instrument_order_ids(
        self,
        exchange_id: str,
        symbol: str,
    ) -> set[str]:
        """读取指定合约的全部活动订单编号。"""

        return self.redis_client.smembers(
            instrument_active_orders_key(exchange_id, symbol)
        )

    def list_account_order_ids(self, account_id: str) -> set[str]:
        """读取指定账户的全部活动订单编号。"""

        return self.redis_client.smembers(
            account_active_orders_key(account_id)
        )

    def list_all_order_ids(self) -> set[str]:
        """从全局Set读取全部活动订单编号，不使用Redis KEYS扫描。"""

        return self.redis_client.smembers(ACTIVE_ORDERS_ALL_KEY)

    def remove_orphan_order_id(self, order_id: str) -> None:
        """
        清理详情Hash已经缺失的孤立全局成员。

        因无法得知原账户和合约，本方法只清理active_orders:all；不使用KEYS
        扫描未知Set，调用方应记录警告并交由后续索引对账处理。
        """

        self.redis_client.srem(ACTIVE_ORDERS_ALL_KEY, order_id)
