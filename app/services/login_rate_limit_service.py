import hashlib

from redis.exceptions import RedisError

from app.common.exceptions import RateLimitError, ServiceUnavailableError
from app.core.config import settings


INCREMENT_LOGIN_RATE_SCRIPT = """
local value = redis.call('INCR', KEYS[1])
if value == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return value
"""


class LoginRateLimitService:
    """基于Redis的IP分钟窗口限流；Redis故障时登录失败关闭。"""

    def __init__(self, redis_client, *, limit: int | None = None):
        self.redis_client = redis_client
        self.limit = limit or settings.auth_login_rate_limit_per_minute

    def check(self, client_ip: str) -> None:
        digest = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()[:32]
        key = f"auth:login-rate:{digest}"
        try:
            count = int(
                self.redis_client.eval(
                    INCREMENT_LOGIN_RATE_SCRIPT,
                    1,
                    key,
                    60,
                )
            )
        except RedisError as exc:
            raise ServiceUnavailableError(
                "登录安全检查暂时不可用",
                error_code="LOGIN_PROTECTION_UNAVAILABLE",
            ) from exc
        if count > self.limit:
            raise RateLimitError(
                "登录请求过于频繁，请稍后重试",
                error_code="LOGIN_RATE_LIMITED",
            )
