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

FENCED_GATEWAY_ACK_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return {0, 0}
end
local message_ids = {}
for index = 3, #ARGV do
    message_ids[#message_ids + 1] = ARGV[index]
end
local acknowledged = redis.call(
    'XACK', KEYS[2], ARGV[2], unpack(message_ids)
)
return {1, acknowledged}
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

    def is_owner(self, owner_id: str) -> bool:
        """读取当前owner；消费和路由前用它尽早发现租约漂移。"""

        value = self.redis_client.get(self.key)
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return value == owner_id

    def acknowledge_if_owned(
        self,
        *,
        owner_id: str,
        stream_name: str,
        group_name: str,
        message_ids: list[str],
    ) -> tuple[bool, int]:
        """仅当前owner仍持有租约时原子XACK，防止旧实例吞消息。"""

        if not message_ids:
            return self.is_owner(owner_id), 0
        result = self.redis_client.eval(
            FENCED_GATEWAY_ACK_SCRIPT,
            2,
            self.key,
            stream_name,
            owner_id,
            group_name,
            *message_ids,
        )
        return bool(int(result[0])), int(result[1])
