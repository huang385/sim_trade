from datetime import datetime, timezone
from urllib.parse import quote, unquote

from redis import Redis

from app.common.exceptions import BusinessRuleError
from app.infrastructure.redis_keys import MARKET_CLIENT_SUBSCRIPTIONS_KEY


UPSERT_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local prefix = ARGV[2]
local current = redis.call('ZRANGE', KEYS[1], 0, -1)
local owned = {}
local count = 0
for _, member in ipairs(current) do
    if string.sub(member, 1, string.len(prefix)) == prefix then
        owned[member] = true
        count = count + 1
    end
end
for index = 5, #ARGV do
    if not owned[ARGV[index]] then count = count + 1 end
end
if count > tonumber(ARGV[3]) then return -1 end
for index = 5, #ARGV do
    redis.call('ZADD', KEYS[1], ARGV[4], ARGV[index])
end
return count
"""

LIST_ACTIVE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
return redis.call('ZRANGE', KEYS[1], 0, -1)
"""

REMOVE_CONNECTION_SCRIPT = """
local prefix = ARGV[1]
local current = redis.call('ZRANGE', KEYS[1], 0, -1)
local removed = 0
for _, member in ipairs(current) do
    if string.sub(member, 1, string.len(prefix)) == prefix then
        removed = removed + redis.call('ZREM', KEYS[1], member)
    end
end
return removed
"""


class ClientMarketSubscriptionStore:
    """跨进程保存客户端行情需求；Redis中只保存连接编号和标准合约代码。"""

    def __init__(
        self,
        redis_client: Redis,
        *,
        key: str = MARKET_CLIENT_SUBSCRIPTIONS_KEY,
        ttl_seconds: int = 90,
        max_codes_per_connection: int = 50,
        now_provider=None,
    ) -> None:
        if ttl_seconds <= 0 or max_codes_per_connection <= 0:
            raise ValueError("客户端行情订阅配置必须为正数")
        self.redis_client = redis_client
        self.key = key
        self.ttl_seconds = ttl_seconds
        self.max_codes_per_connection = max_codes_per_connection
        self.now_provider = now_provider or (
            lambda: datetime.now(timezone.utc)
        )

    @staticmethod
    def _connection_prefix(connection_id: str) -> str:
        return f"{quote(connection_id.strip(), safe='')}|"

    @classmethod
    def _member(cls, connection_id: str, code: str) -> str:
        return (
            f"{cls._connection_prefix(connection_id)}"
            f"{quote(code.strip().upper(), safe='')}"
        )

    @staticmethod
    def _text(value: str | bytes) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else value

    def request_codes(self, *, connection_id: str, codes: set[str]) -> datetime:
        normalized = sorted({code.strip().upper() for code in codes if code.strip()})
        if not normalized:
            raise ValueError("客户端行情订阅不能为空")
        now = self.now_provider()
        expires_at = now.timestamp() + self.ttl_seconds
        result = self.redis_client.eval(
            UPSERT_SCRIPT,
            1,
            self.key,
            format(now.timestamp(), ".6f"),
            self._connection_prefix(connection_id),
            self.max_codes_per_connection,
            format(expires_at, ".6f"),
            *(self._member(connection_id, code) for code in normalized),
        )
        if int(result) < 0:
            raise BusinessRuleError(
                "当前连接的行情订阅数量超过限制",
                error_code="MARKET_SUBSCRIPTION_LIMIT_EXCEEDED",
            )
        return datetime.fromtimestamp(expires_at, tz=timezone.utc)

    def remove_codes(self, *, connection_id: str, codes: set[str]) -> int:
        members = [self._member(connection_id, code) for code in codes]
        return int(self.redis_client.zrem(self.key, *members)) if members else 0

    def remove_connection(self, connection_id: str) -> int:
        return int(
            self.redis_client.eval(
                REMOVE_CONNECTION_SCRIPT,
                1,
                self.key,
                self._connection_prefix(connection_id),
            )
        )

    def list_active_contract_codes(self) -> set[str]:
        now = self.now_provider()
        members = self.redis_client.eval(
            LIST_ACTIVE_SCRIPT,
            1,
            self.key,
            format(now.timestamp(), ".6f"),
        ) or []
        return {
            unquote(self._text(member).split("|", 1)[1])
            for member in members
        }
