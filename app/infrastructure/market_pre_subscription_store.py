from datetime import datetime, timezone
from urllib.parse import quote, unquote

from redis import Redis

from app.common.exceptions import BusinessRuleError
from app.infrastructure.redis_keys import MARKET_PRE_SUBSCRIPTIONS_KEY


REQUEST_CODES_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local current = redis.call('ZRANGE', KEYS[1], 0, -1)
local count = 0
for _, member in ipairs(current) do
    if string.sub(member, 1, string.len(ARGV[2])) == ARGV[2] then
        count = count + 1
    end
end
for index = 5, #ARGV do
    if redis.call('ZSCORE', KEYS[1], ARGV[index]) == false then
        count = count + 1
    end
end
if count > tonumber(ARGV[3]) then
    return -1
end
for index = 5, #ARGV do
    redis.call('ZADD', KEYS[1], ARGV[4], ARGV[index])
end
return count
"""


LIST_ACTIVE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
return redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')
"""


class MarketPreSubscriptionStore:
    """保存按账户隔离、按成员自动失效的临时行情订阅需求。"""

    def __init__(
        self,
        redis_client: Redis,
        *,
        key: str = MARKET_PRE_SUBSCRIPTIONS_KEY,
        ttl_seconds: int = 60,
        max_codes_per_account: int = 20,
        now_provider=None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("行情预订阅TTL必须大于0")
        if max_codes_per_account <= 0:
            raise ValueError("账户预订阅合约上限必须大于0")
        self.redis_client = redis_client
        self.key = key
        self.ttl_seconds = ttl_seconds
        self.max_codes_per_account = max_codes_per_account
        self.now_provider = now_provider or (
            lambda: datetime.now(timezone.utc)
        )

    @staticmethod
    def _prefix(account_id: str) -> str:
        return f"{quote(account_id.strip(), safe='')}|"

    @classmethod
    def _member(cls, account_id: str, code: str) -> str:
        return f"{cls._prefix(account_id)}{quote(code.strip().upper(), safe='')}"

    @staticmethod
    def _parse_member(member: str) -> tuple[str, str]:
        account_id, code = member.split("|", 1)
        return unquote(account_id), unquote(code)

    @staticmethod
    def _text(value: str | bytes) -> str:
        """兼容Redis启用或关闭decode_responses时的返回类型。"""

        return value.decode("utf-8") if isinstance(value, bytes) else value

    def request_codes(
        self,
        *,
        account_id: str,
        codes: set[str] | frozenset[str] | list[str] | tuple[str, ...],
    ) -> datetime:
        """原子刷新一组代码的有效期，并限制单账户临时订阅规模。"""

        normalized = sorted(
            {
                str(code).strip().upper()
                for code in codes
                if str(code).strip()
            }
        )
        if not normalized:
            raise ValueError("预订阅代码不能为空")
        now = self.now_provider()
        expires_at = now.timestamp() + self.ttl_seconds
        result = self.redis_client.eval(
            REQUEST_CODES_SCRIPT,
            1,
            self.key,
            format(now.timestamp(), ".6f"),
            self._prefix(account_id),
            self.max_codes_per_account,
            format(expires_at, ".6f"),
            *(self._member(account_id, code) for code in normalized),
        )
        if int(result) < 0:
            raise BusinessRuleError(
                "账户临时行情订阅数量超过限制",
                error_code="MARKET_PRE_SUBSCRIPTION_LIMIT_EXCEEDED",
            )
        return datetime.fromtimestamp(expires_at, tz=timezone.utc)

    def _active_rows(self) -> list[tuple[str, str, datetime]]:
        now = self.now_provider()
        values = self.redis_client.eval(
            LIST_ACTIVE_SCRIPT,
            1,
            self.key,
            format(now.timestamp(), ".6f"),
        ) or []
        rows: list[tuple[str, str, datetime]] = []
        for index in range(0, len(values), 2):
            account_id, code = self._parse_member(
                self._text(values[index])
            )
            rows.append(
                (
                    account_id,
                    code,
                    datetime.fromtimestamp(
                        float(self._text(values[index + 1])),
                        tz=timezone.utc,
                    ),
                )
            )
        return rows

    def list_active_contract_codes(self) -> set[str]:
        """返回所有账户仍有效的标准合约代码，重复代码自动去重。"""

        return {code for _account_id, code, _expires_at in self._active_rows()}

    def get_account_requests(self, account_id: str) -> dict[str, datetime]:
        """读取指定账户仍有效的代码及各自到期时间。"""

        normalized = account_id.strip()
        return {
            code: expires_at
            for owner, code, expires_at in self._active_rows()
            if owner == normalized
        }
