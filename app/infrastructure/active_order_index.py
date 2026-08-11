from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from redis import Redis

from app.infrastructure.redis_keys import (
    ACTIVE_ORDER_CONTRACTS_KEY,
    ACTIVE_ORDERS_ALL_KEY,
    account_active_orders_key,
    active_order_contract_member,
    active_order_key,
    instrument_active_orders_key,
    underlying_sell_open_orders_key,
    parse_active_order_contract_member,
    processed_order_event_key,
)


# 一个Lua脚本同时写详情、订单倒排集合、活动合约集合和事件幂等标记。
# 这样即使 Consumer 在 Redis 操作中途退出，也不会留下只写了一半的索引。
ADD_ACTIVE_ORDER_SCRIPT = """
local is_new_event = 0
if redis.call('EXISTS', KEYS[5]) == 0 then
    is_new_event = 1
end
redis.call('HSET', KEYS[1], unpack(ARGV, 4))
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('SADD', KEYS[3], ARGV[1])
redis.call('SADD', KEYS[4], ARGV[1])
redis.call('SADD', KEYS[6], ARGV[3])
if KEYS[7] ~= '' then
    redis.call('SADD', KEYS[7], ARGV[1])
end
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
if KEYS[7] ~= '' then
    redis.call('SREM', KEYS[7], ARGV[1])
end
if redis.call('SCARD', KEYS[2]) == 0 and ARGV[3] ~= '' then
    redis.call('SREM', KEYS[6], ARGV[3])
end
if KEYS[5] ~= '' then
    redis.call('SET', KEYS[5], '1', 'EX', ARGV[2])
end
return 1
"""


# 重建不属于Stream事件处理：覆盖Hash和派生Set，不写processed事件标记。
UPSERT_ACTIVE_ORDER_FOR_REBUILD_SCRIPT = """
redis.call('HSET', KEYS[1], unpack(ARGV, 3))
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('SADD', KEYS[3], ARGV[1])
redis.call('SADD', KEYS[4], ARGV[1])
redis.call('SADD', KEYS[5], ARGV[2])
if KEYS[6] ~= '' then
    redis.call('SADD', KEYS[6], ARGV[1])
end
return 1
"""


REMOVE_EMPTY_ACTIVE_CONTRACT_SCRIPT = """
if redis.call('SCARD', KEYS[1]) == 0 then
    return redis.call('SREM', KEYS[2], ARGV[1])
end
return 0
"""


ACTIVE_ORDER_FIELDS = (
    "order_id",
    "account_id",
    "exchange_id",
    "symbol",
    "order_book_id",
    "instrument_type",
    "underlying_order_book_id",
    "underlying_exchange_id",
    "underlying_symbol",
    "direction",
    "offset_flag",
    "order_type",
    "limit_price",
    "submitted_limit_price",
    "resolved_price",
    "market_protection_price",
    "price_snapshot_time",
    "price_snapshot_source",
    "price_snapshot_bid1",
    "price_snapshot_ask1",
    "price_snapshot_last",
    "total_volume",
    "traded_volume",
    "remaining_volume",
    "cancelled_volume",
    "average_price",
    "frozen_margin",
    "frozen_cash",
    "frozen_commission",
    "frozen_position_volume",
    "margin_risk_state",
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

    @staticmethod
    def _underlying_dependency_key(order: Any) -> str:
        """卖出开仓期权需要同时监听期权与标的行情变化。"""

        if (
            str(getattr(order, "instrument_type", ""))
            in {"FUTURES_OPTION", "INDEX_OPTION"}
            and str(getattr(order, "direction", "")) == "SELL"
            and str(getattr(order, "offset_flag", "")) == "OPEN"
            and getattr(order, "underlying_exchange_id", None)
            and getattr(order, "underlying_symbol", None)
        ):
            return underlying_sell_open_orders_key(
                str(order.underlying_exchange_id),
                str(order.underlying_symbol),
            )
        return ""

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
            7,
            active_order_key(order.order_id),
            instrument_active_orders_key(order.exchange_id, order.symbol),
            account_active_orders_key(order.account_id),
            ACTIVE_ORDERS_ALL_KEY,
            processed_order_event_key(event_id),
            ACTIVE_ORDER_CONTRACTS_KEY,
            self._underlying_dependency_key(order),
            order.order_id,
            processed_ttl_seconds,
            active_order_contract_member(
                order.exchange_id,
                order.symbol,
                getattr(order, "order_book_id", order.symbol),
            ),
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
            6,
            active_order_key(order.order_id),
            instrument_active_orders_key(order.exchange_id, order.symbol),
            account_active_orders_key(order.account_id),
            ACTIVE_ORDERS_ALL_KEY,
            ACTIVE_ORDER_CONTRACTS_KEY,
            self._underlying_dependency_key(order),
            order.order_id,
            active_order_contract_member(
                order.exchange_id,
                order.symbol,
                getattr(order, "order_book_id", order.symbol),
            ),
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
        detail = self.get_active_order(order_id)
        contract_member = ""
        if detail.get("order_book_id"):
            contract_member = active_order_contract_member(
                exchange_id,
                symbol,
                detail["order_book_id"],
            )
        dependency_key = ""
        if (
            detail.get("instrument_type")
            in {"FUTURES_OPTION", "INDEX_OPTION"}
            and detail.get("direction") == "SELL"
            and detail.get("offset_flag") == "OPEN"
            and detail.get("underlying_exchange_id")
            and detail.get("underlying_symbol")
        ):
            dependency_key = underlying_sell_open_orders_key(
                detail["underlying_exchange_id"],
                detail["underlying_symbol"],
            )
        self.redis_client.eval(
            REMOVE_ACTIVE_ORDER_SCRIPT,
            7,
            active_order_key(order_id),
            instrument_active_orders_key(exchange_id, symbol),
            account_active_orders_key(account_id),
            ACTIVE_ORDERS_ALL_KEY,
            processed_key,
            ACTIVE_ORDER_CONTRACTS_KEY,
            dependency_key,
            order_id,
            processed_ttl_seconds,
            contract_member,
        )

    def get_active_order(self, order_id: str) -> dict[str, str]:
        """读取单笔活动订单详情；不存在时返回空字典。"""

        return self.redis_client.hgetall(active_order_key(order_id))

    def update_margin_risk_snapshot(
        self,
        *,
        order_id: str,
        margin_risk_state: str,
        frozen_margin: Decimal | None = None,
    ) -> None:
        """更新活动索引中的派生风险快照；PostgreSQL仍是最终事实来源。"""

        mapping = {"margin_risk_state": margin_risk_state}
        if frozen_margin is not None:
            mapping["frozen_margin"] = serialize_redis_value(frozen_margin)
        self.redis_client.hset(active_order_key(order_id), mapping=mapping)

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

    def list_underlying_sell_open_order_ids(
        self,
        exchange_id: str,
        symbol: str,
    ) -> set[str]:
        """读取标的行情变化时需要重新估值的活动期权卖出开仓订单。"""

        return self.redis_client.smembers(
            underlying_sell_open_orders_key(exchange_id, symbol)
        )

    def list_all_order_ids(self) -> set[str]:
        """从全局Set读取全部活动订单编号，不使用Redis KEYS扫描。"""

        return self.redis_client.smembers(ACTIVE_ORDERS_ALL_KEY)

    def list_active_contract_codes(self) -> set[str]:
        """一次读取全部活动订单订阅代码，不再逐订单读取详情Hash。"""

        codes: set[str] = set()
        for member in self.redis_client.smembers(
            ACTIVE_ORDER_CONTRACTS_KEY
        ):
            try:
                _exchange_id, _symbol, order_book_id = (
                    parse_active_order_contract_member(str(member))
                )
            except (TypeError, ValueError):
                continue
            if order_book_id:
                codes.add(order_book_id)
        return codes

    def list_margin_dependency_codes(self) -> set[str]:
        """
        批量读取活动卖出开仓期权依赖的标的订阅代码。

        全部字段通过一个非事务Pipeline读取，不产生逐订单网络往返；订单
        Hash仍由现有活动订单索引维护，不引入第二套订单事实结构。
        """

        order_ids = sorted(self.list_all_order_ids())
        if not order_ids:
            return set()
        pipeline = self.redis_client.pipeline(transaction=False)
        for order_id in order_ids:
            pipeline.hmget(
                active_order_key(str(order_id)),
                (
                    "instrument_type",
                    "direction",
                    "offset_flag",
                    "underlying_order_book_id",
                ),
            )
        codes: set[str] = set()
        for values in pipeline.execute():
            instrument_type, direction, offset_flag, underlying_code = (
                values or (None, None, None, None)
            )
            if (
                str(instrument_type or "")
                in {"FUTURES_OPTION", "INDEX_OPTION"}
                and str(direction or "") == "SELL"
                and str(offset_flag or "") == "OPEN"
                and underlying_code
            ):
                codes.add(str(underlying_code))
        return codes

    def reconcile_active_contracts(self) -> int:
        """
        重建结束后原子清除对应合约订单Set已经为空的派生成员。

        检查SCARD和删除成员位于同一Lua中，并发新订单要么先加入、要么在清理
        后重新加入，因此不会把仍有活动订单的合约永久误删。
        """

        removed = 0
        members = sorted(
            self.redis_client.smembers(ACTIVE_ORDER_CONTRACTS_KEY)
        )
        for member in members:
            try:
                exchange_id, symbol, _order_book_id = (
                    parse_active_order_contract_member(str(member))
                )
            except (TypeError, ValueError):
                self.redis_client.srem(
                    ACTIVE_ORDER_CONTRACTS_KEY,
                    member,
                )
                removed += 1
                continue
            removed += int(
                self.redis_client.eval(
                    REMOVE_EMPTY_ACTIVE_CONTRACT_SCRIPT,
                    2,
                    instrument_active_orders_key(
                        exchange_id,
                        symbol,
                    ),
                    ACTIVE_ORDER_CONTRACTS_KEY,
                    member,
                )
                or 0
            )
        return removed

    def remove_orphan_order_id(self, order_id: str) -> None:
        """
        清理详情Hash已经缺失的孤立全局成员。

        因无法得知原账户和合约，本方法只清理active_orders:all；不使用KEYS
        扫描未知Set，调用方应记录警告并交由后续索引对账处理。
        """

        self.redis_client.srem(ACTIVE_ORDERS_ALL_KEY, order_id)
