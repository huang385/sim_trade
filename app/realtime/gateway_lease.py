from redis import Redis

from app.core.config import settings
from app.infrastructure.redis_keys import WS_GATEWAY_LEASE_KEY


RENEW_GATEWAY_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

RELEASE_GATEWAY_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class GatewayLease:
    """Redis所有者租约，保证第一版只有一个活动Gateway。"""

    def __init__(
        self,
        redis_client: Redis,
        *,
        key: str = WS_GATEWAY_LEASE_KEY,
        ttl_seconds: int | None = None,
    ):
        self.redis_client = redis_client
        self.key = key
        self.ttl_seconds = ttl_seconds or settings.ws_gateway_lease_ttl_seconds

    def acquire(self, owner_id: str) -> bool:
        return bool(
            self.redis_client.set(
                self.key,
                owner_id,
                nx=True,
                ex=self.ttl_seconds,
            )
        )

    def renew(self, owner_id: str) -> bool:
        return bool(
            self.redis_client.eval(
                RENEW_GATEWAY_LEASE_SCRIPT,
                1,
                self.key,
                owner_id,
                self.ttl_seconds,
            )
        )

    def release(self, owner_id: str) -> bool:
        return bool(
            self.redis_client.eval(
                RELEASE_GATEWAY_LEASE_SCRIPT,
                1,
                self.key,
                owner_id,
            )
        )
