"""Redis trigger state for cash-security valuation.

Redis only routes work.  Prices, positions and money are recomputed from the
locked PostgreSQL facts by :mod:`cash_security_valuation_service`.
"""

from collections.abc import Iterable

from redis import Redis

from app.infrastructure.redis_keys import (
    CASH_VALUATION_DIRTY_ACCOUNTS_KEY,
    CASH_VALUATION_DIRTY_ACCOUNT_VERSIONS_KEY,
    CASH_VALUATION_DIRTY_SEQUENCE_KEY,
    CASH_VALUATION_INDEX_KEYS_KEY,
    CASH_VALUATION_POSITION_ACCOUNTS_KEY,
    CASH_VALUATION_POSITION_INDEX_KEYS_KEY,
    CASH_VALUATION_WORKER_FENCE_KEY,
    CASH_VALUATION_WORKER_LEASE_KEY,
    cash_valuation_instrument_positions_key,
)

_INSTRUMENT_INDEX_PREFIX = cash_valuation_instrument_positions_key("", "")[:-1]


_COMPLETE_DIRTY_SCRIPT = """
if redis.call('HGET', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('SREM', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
return 1
"""

_MARK_DIRTY_SCRIPT = """
local result = {}
for index = 2, #ARGV do
    local account_id = ARGV[index]
    local version = redis.call('INCR', KEYS[3])
    local value = tostring(version) .. ':' .. ARGV[1]
    redis.call('SADD', KEYS[1], account_id)
    redis.call('HSET', KEYS[2], account_id, value)
    table.insert(result, account_id)
    table.insert(result, value)
end
return result
"""

_ACQUIRE_LEASE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then return '' end
local token = redis.call('INCR', KEYS[2])
local value = ARGV[1] .. ':' .. tostring(token)
redis.call('SET', KEYS[1], value, 'EX', ARGV[2])
return tostring(token)
"""

_RENEW_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


class CashSecurityValuationStore:
    """Rebuildable active-position index and versioned cash dirty queue."""

    def __init__(self, redis_client: Redis) -> None:
        self.redis_client = redis_client

    @staticmethod
    def _key(exchange_id: str, order_book_id: str) -> str:
        return cash_valuation_instrument_positions_key(exchange_id, order_book_id)

    def rebuild_active_positions(
        self,
        positions: Iterable[tuple[str, str, str, str]],
    ) -> None:
        """Atomically replace the cache from PostgreSQL active cash positions.

        Each tuple is ``(position_id, account_id, exchange_id, order_book_id)``.
        No market value is kept here, so this operation is safe after a Redis
        restart and cannot corrupt durable valuation facts.
        """

        rows = list(positions)
        old_keys = list(self.redis_client.smembers(CASH_VALUATION_INDEX_KEYS_KEY))
        pipe = self.redis_client.pipeline(transaction=True)
        if old_keys:
            pipe.delete(*old_keys)
        pipe.delete(
            CASH_VALUATION_INDEX_KEYS_KEY,
            CASH_VALUATION_POSITION_ACCOUNTS_KEY,
            CASH_VALUATION_POSITION_INDEX_KEYS_KEY,
        )
        by_key: dict[str, dict[str, str]] = {}
        for position_id, account_id, exchange_id, order_book_id in rows:
            key = self._key(exchange_id, order_book_id)
            by_key.setdefault(key, {})[position_id] = account_id
        for key, members in by_key.items():
            pipe.sadd(CASH_VALUATION_INDEX_KEYS_KEY, key)
            pipe.sadd(key, *members)
            pipe.hset(CASH_VALUATION_POSITION_ACCOUNTS_KEY, mapping=members)
            pipe.hset(
                CASH_VALUATION_POSITION_INDEX_KEYS_KEY,
                mapping={position_id: key for position_id in members},
            )
        pipe.execute()

    def upsert_active_position(
        self, *, position_id: str, account_id: str, exchange_id: str, order_book_id: str
    ) -> None:
        """Update one routing entry after a position fact; no table-wide rebuild."""

        new_key = self._key(exchange_id, order_book_id)
        old_key = self.redis_client.hget(
            CASH_VALUATION_POSITION_INDEX_KEYS_KEY, position_id
        )
        pipe = self.redis_client.pipeline(transaction=True)
        if old_key and old_key != new_key:
            pipe.srem(old_key, position_id)
        pipe.sadd(new_key, position_id)
        pipe.sadd(CASH_VALUATION_INDEX_KEYS_KEY, new_key)
        pipe.hset(CASH_VALUATION_POSITION_ACCOUNTS_KEY, position_id, account_id)
        pipe.hset(CASH_VALUATION_POSITION_INDEX_KEYS_KEY, position_id, new_key)
        pipe.execute()

    def remove_active_position(self, position_id: str) -> None:
        """Remove one closed position while retaining unrelated index members."""

        key = self.redis_client.hget(
            CASH_VALUATION_POSITION_INDEX_KEYS_KEY, position_id
        )
        pipe = self.redis_client.pipeline(transaction=True)
        if key:
            pipe.srem(key, position_id)
        pipe.hdel(CASH_VALUATION_POSITION_ACCOUNTS_KEY, position_id)
        pipe.hdel(CASH_VALUATION_POSITION_INDEX_KEYS_KEY, position_id)
        pipe.execute()

    def account_ids_for_tick(self, *, exchange_id: str, order_book_id: str) -> set[str]:
        position_ids = self.redis_client.smembers(self._key(exchange_id, order_book_id))
        if not position_ids:
            return set()
        values = self.redis_client.hmget(CASH_VALUATION_POSITION_ACCOUNTS_KEY, list(position_ids))
        return {value for value in values if value}

    def list_active_contract_codes(self) -> set[str]:
        """返回当前至少包含一条有效现金证券持仓的行情代码集合。

        供行情订阅服务把股票/可转债持仓与期货持仓一样纳入订阅目标。
        索引键保存时已对 order_book_id 归一化，这里解析出的代码可以直接
        交给 normalize_code 与合约代码映射使用。
        """

        raw_keys = list(self.redis_client.smembers(CASH_VALUATION_INDEX_KEYS_KEY))
        index_keys = [
            value.decode("utf-8") if isinstance(value, bytes) else str(value)
            for value in raw_keys
        ]
        instrument_keys = [
            key for key in index_keys if key.startswith(_INSTRUMENT_INDEX_PREFIX)
        ]
        if not instrument_keys:
            return set()
        pipeline = self.redis_client.pipeline(transaction=False)
        for key in instrument_keys:
            pipeline.scard(key)
        counts = pipeline.execute()
        codes: set[str] = set()
        for key, count in zip(instrument_keys, counts, strict=True):
            if int(count or 0) <= 0:
                continue
            suffix = key[len(_INSTRUMENT_INDEX_PREFIX):]
            if ":" in suffix:
                codes.add(suffix.split(":", 1)[1])
        return codes

    @staticmethod
    def list_margin_dependency_codes() -> set[str]:
        """现金证券没有保证金依赖标的，协议方法固定返回空集合。"""

        return set()

    def mark_accounts_dirty(self, account_ids: Iterable[str], *, reason: str) -> dict[str, str]:
        """Assign fresh monotonic versions; duplicate messages only schedule work."""

        ids = sorted({str(item).strip() for item in account_ids if str(item).strip()})
        if not ids:
            return {}
        rows = self.redis_client.eval(
            _MARK_DIRTY_SCRIPT,
            3,
            CASH_VALUATION_DIRTY_ACCOUNTS_KEY,
            CASH_VALUATION_DIRTY_ACCOUNT_VERSIONS_KEY,
            CASH_VALUATION_DIRTY_SEQUENCE_KEY,
            reason,
            *ids,
        )
        return dict(zip(rows[::2], rows[1::2], strict=True))

    def list_dirty_accounts(self, batch_size: int) -> list[tuple[str, str]]:
        ids = sorted(self.redis_client.smembers(CASH_VALUATION_DIRTY_ACCOUNTS_KEY))[:batch_size]
        if not ids:
            return []
        versions = self.redis_client.hmget(CASH_VALUATION_DIRTY_ACCOUNT_VERSIONS_KEY, ids)
        return [(account_id, version) for account_id, version in zip(ids, versions, strict=True) if version]

    def complete_dirty_account(self, *, account_id: str, expected_version: str) -> bool:
        return bool(self.redis_client.eval(
            _COMPLETE_DIRTY_SCRIPT, 2, CASH_VALUATION_DIRTY_ACCOUNTS_KEY,
            CASH_VALUATION_DIRTY_ACCOUNT_VERSIONS_KEY, account_id, expected_version,
        ))

    @staticmethod
    def _lease_value(owner: str, fencing_token: str) -> str:
        return f"{owner}:{fencing_token}"

    def writer_lease_value(self, owner: str, fencing_token: str) -> str:
        return self._lease_value(owner, fencing_token)

    def acquire_writer_lease(self, owner: str, ttl_seconds: int) -> str | None:
        token = self.redis_client.eval(
            _ACQUIRE_LEASE_SCRIPT,
            2,
            CASH_VALUATION_WORKER_LEASE_KEY,
            CASH_VALUATION_WORKER_FENCE_KEY,
            owner,
            max(ttl_seconds, 1),
        )
        return str(token) if token else None

    def renew_writer_lease(
        self, owner: str, fencing_token: str, ttl_seconds: int
    ) -> bool:
        return bool(self.redis_client.eval(
            _RENEW_LEASE_SCRIPT,
            1,
            CASH_VALUATION_WORKER_LEASE_KEY,
            self._lease_value(owner, fencing_token),
            max(ttl_seconds, 1),
        ))

    def writer_lease_owned(self, owner: str, fencing_token: str) -> bool:
        return self.redis_client.get(CASH_VALUATION_WORKER_LEASE_KEY) == self._lease_value(owner, fencing_token)

    def release_writer_lease(self, owner: str, fencing_token: str) -> bool:
        return bool(self.redis_client.eval(
            _RELEASE_LEASE_SCRIPT,
            1,
            CASH_VALUATION_WORKER_LEASE_KEY,
            self._lease_value(owner, fencing_token),
        ))
