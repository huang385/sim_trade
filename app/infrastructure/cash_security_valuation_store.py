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
    cash_valuation_instrument_positions_key,
)


_COMPLETE_DIRTY_SCRIPT = """
if redis.call('HGET', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('SREM', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
return 1
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
        pipe.delete(CASH_VALUATION_INDEX_KEYS_KEY, CASH_VALUATION_POSITION_ACCOUNTS_KEY)
        by_key: dict[str, dict[str, str]] = {}
        for position_id, account_id, exchange_id, order_book_id in rows:
            key = self._key(exchange_id, order_book_id)
            by_key.setdefault(key, {})[position_id] = account_id
        for key, members in by_key.items():
            pipe.sadd(CASH_VALUATION_INDEX_KEYS_KEY, key)
            pipe.sadd(key, *members)
            pipe.hset(CASH_VALUATION_POSITION_ACCOUNTS_KEY, mapping=members)
        pipe.execute()

    def account_ids_for_tick(self, *, exchange_id: str, order_book_id: str) -> set[str]:
        position_ids = self.redis_client.smembers(self._key(exchange_id, order_book_id))
        if not position_ids:
            return set()
        values = self.redis_client.hmget(CASH_VALUATION_POSITION_ACCOUNTS_KEY, list(position_ids))
        return {value for value in values if value}

    def mark_accounts_dirty(self, account_ids: Iterable[str], *, reason: str) -> dict[str, str]:
        """Assign fresh monotonic versions; duplicate messages only schedule work."""

        ids = sorted({str(item).strip() for item in account_ids if str(item).strip()})
        if not ids:
            return {}
        pipe = self.redis_client.pipeline(transaction=True)
        for account_id in ids:
            pipe.incr(CASH_VALUATION_DIRTY_SEQUENCE_KEY)
        versions = [str(item) for item in pipe.execute()]
        pipe = self.redis_client.pipeline(transaction=True)
        for account_id, sequence in zip(ids, versions, strict=True):
            version = f"{sequence}:{reason}"
            pipe.sadd(CASH_VALUATION_DIRTY_ACCOUNTS_KEY, account_id)
            pipe.hset(CASH_VALUATION_DIRTY_ACCOUNT_VERSIONS_KEY, account_id, version)
        pipe.execute()
        return dict(zip(ids, (f"{seq}:{reason}" for seq in versions), strict=True))

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
