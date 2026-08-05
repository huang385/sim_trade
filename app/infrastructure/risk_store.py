from collections.abc import Iterable

from redis import Redis

from app.infrastructure.redis_keys import (
    RISK_DIRTY_ACCOUNTS_KEY,
    RISK_DIRTY_ACCOUNT_VERSIONS_KEY,
    RISK_DIRTY_SCAN_CURSOR_KEY,
    RISK_WORKER_LEASE_KEY,
    processed_risk_trigger_key,
)


MARK_DIRTY_SCRIPT = """
local version = redis.call('HINCRBY', KEYS[1], ARGV[1], 1)
redis.call('SADD', KEYS[2], ARGV[1])
return tostring(version)
"""

MARK_DIRTY_ONCE_SCRIPT = """
if redis.call('EXISTS', KEYS[3]) == 1 then return '' end
local version = redis.call('HINCRBY', KEYS[1], ARGV[1], 1)
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('SET', KEYS[3], tostring(version), 'EX', ARGV[2])
return tostring(version)
"""

CLEAR_DIRTY_SCRIPT = """
if redis.call('HGET', KEYS[1], ARGV[1]) == ARGV[2] then
    redis.call('SREM', KEYS[2], ARGV[1])
    return 1
end
return 0
"""

RENEW_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

RELEASE_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class RiskStore:
    """账户风险Dirty、CAS版本和单写者租约的Redis适配器。"""

    def __init__(self, redis_client: Redis):
        self.redis_client = redis_client

    def mark_dirty(self, account_id: str) -> str:
        return str(
            self.redis_client.eval(
                MARK_DIRTY_SCRIPT,
                2,
                RISK_DIRTY_ACCOUNT_VERSIONS_KEY,
                RISK_DIRTY_ACCOUNTS_KEY,
                account_id,
            )
        )

    def mark_dirty_once(
        self, *, account_id: str, event_id: str, ttl_seconds: int = 604800
    ) -> str:
        return str(
            self.redis_client.eval(
                MARK_DIRTY_ONCE_SCRIPT,
                3,
                RISK_DIRTY_ACCOUNT_VERSIONS_KEY,
                RISK_DIRTY_ACCOUNTS_KEY,
                processed_risk_trigger_key(event_id),
                account_id,
                ttl_seconds,
            )
            or ""
        )

    def mark_many_dirty(self, account_ids: Iterable[str]) -> int:
        count = 0
        for account_id in dict.fromkeys(account_ids):
            self.mark_dirty(account_id)
            count += 1
        return count

    def list_dirty(self, batch_size: int) -> list[tuple[str, str]]:
        """复用持久Redis游标轮转SSCAN，坏账户不能饿死后续账户。"""

        if batch_size <= 0:
            return []
        raw_cursor = self.redis_client.get(RISK_DIRTY_SCAN_CURSOR_KEY)
        try:
            cursor = int(raw_cursor or 0)
        except (TypeError, ValueError):
            cursor = 0
        cursor, members = self.redis_client.sscan(
            RISK_DIRTY_ACCOUNTS_KEY, cursor=cursor, count=batch_size
        )
        self.redis_client.set(RISK_DIRTY_SCAN_CURSOR_KEY, str(cursor))
        account_ids = list(dict.fromkeys(members))[:batch_size]
        if not account_ids:
            return []
        versions = self.redis_client.hmget(
            RISK_DIRTY_ACCOUNT_VERSIONS_KEY, account_ids
        )
        return [
            (account_id, str(version))
            for account_id, version in zip(account_ids, versions, strict=True)
            if version is not None
        ]

    def complete_dirty(self, account_id: str, expected_version: str) -> bool:
        return bool(
            self.redis_client.eval(
                CLEAR_DIRTY_SCRIPT,
                2,
                RISK_DIRTY_ACCOUNT_VERSIONS_KEY,
                RISK_DIRTY_ACCOUNTS_KEY,
                account_id,
                expected_version,
            )
        )

    def acquire_lease(self, owner: str, ttl_ms: int) -> bool:
        return bool(
            self.redis_client.set(
                RISK_WORKER_LEASE_KEY, owner, nx=True, px=max(ttl_ms, 1)
            )
        )

    def renew_lease(self, owner: str, ttl_ms: int) -> bool:
        return bool(
            self.redis_client.eval(
                RENEW_LEASE_SCRIPT, 1, RISK_WORKER_LEASE_KEY, owner, ttl_ms
            )
        )

    def release_lease(self, owner: str) -> bool:
        return bool(
            self.redis_client.eval(
                RELEASE_LEASE_SCRIPT, 1, RISK_WORKER_LEASE_KEY, owner
            )
        )
