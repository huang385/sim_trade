from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import secrets

from redis import Redis
from redis.exceptions import RedisError

from app.common.exceptions import AuthenticationError, ServiceUnavailableError
from app.common.time_utils import utc_now
from app.core.config import settings
from app.infrastructure.redis_keys import websocket_ticket_key


CONSUME_TICKET_SCRIPT = """
local value = redis.call('GET', KEYS[1])
if not value then
    return nil
end
redis.call('DEL', KEYS[1])
return value
"""


@dataclass(frozen=True)
class WebSocketTicketClaims:
    """一次性Ticket中保存的最小已验证身份上下文。"""

    user_id: str
    role: str
    token_jti: str
    token_expiration: datetime


@dataclass(frozen=True)
class IssuedWebSocketTicket:
    """票据明文只返回调用方一次，同时给出实际有效秒数。"""

    ticket: str
    expires_in: int


class WebSocketTicketService:
    """在Redis中签发并原子消费短期一次性连接Ticket。"""

    def __init__(
        self,
        redis_client: Redis,
        *,
        expire_seconds: int | None = None,
    ):
        self.redis_client = redis_client
        self.expire_seconds = (
            expire_seconds
            if expire_seconds is not None
            else settings.ws_ticket_expire_seconds
        )

    @staticmethod
    def _hash(ticket: str) -> str:
        return hashlib.sha256(ticket.encode("utf-8")).hexdigest()

    def create(
        self,
        *,
        user_id: str,
        role: str,
        token_jti: str,
        token_expiration: datetime,
    ) -> IssuedWebSocketTicket:
        """签发安全随机票据；Redis只保存摘要键和最小身份JSON。"""

        if self.expire_seconds <= 0:
            raise ServiceUnavailableError(
                "WebSocket Ticket配置不合法",
                error_code="WS_TICKET_CONFIG_INVALID",
            )
        remaining_seconds = int(
            (token_expiration - utc_now()).total_seconds()
        )
        actual_ttl = min(self.expire_seconds, remaining_seconds)
        if actual_ttl <= 0:
            raise AuthenticationError(
                "Access Token已过期",
                error_code="INVALID_TOKEN",
            )
        ticket = secrets.token_urlsafe(32)
        key = websocket_ticket_key(self._hash(ticket))
        value = json.dumps(
            {
                "user_id": user_id,
                "role": role,
                "token_jti": token_jti,
                "token_expiration": token_expiration.isoformat(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            created = self.redis_client.set(
                key,
                value,
                ex=actual_ttl,
                nx=True,
            )
        except RedisError as exc:
            raise ServiceUnavailableError(
                "WebSocket认证服务暂不可用",
                error_code="WS_TICKET_STORE_UNAVAILABLE",
            ) from exc
        if not created:
            # 随机碰撞概率可忽略；失败关闭比重复使用现有键更安全。
            raise ServiceUnavailableError(
                "WebSocket Ticket创建失败",
                error_code="WS_TICKET_CREATE_FAILED",
            )
        return IssuedWebSocketTicket(
            ticket=ticket,
            expires_in=actual_ttl,
        )

    def consume(self, ticket: str) -> WebSocketTicketClaims:
        """使用Lua原子GET+DEL，保证并发连接最多一个成功。"""

        normalized = ticket.strip()
        if not normalized:
            raise AuthenticationError(
                "WebSocket Ticket无效或已过期",
                error_code="WS_TICKET_INVALID",
            )
        try:
            raw = self.redis_client.eval(
                CONSUME_TICKET_SCRIPT,
                1,
                websocket_ticket_key(self._hash(normalized)),
            )
        except RedisError as exc:
            raise ServiceUnavailableError(
                "WebSocket认证服务暂不可用",
                error_code="WS_TICKET_STORE_UNAVAILABLE",
            ) from exc
        if not raw:
            raise AuthenticationError(
                "WebSocket Ticket无效或已过期",
                error_code="WS_TICKET_INVALID",
            )
        try:
            values = json.loads(raw)
            claims = WebSocketTicketClaims(
                user_id=str(values["user_id"]),
                role=str(values["role"]),
                token_jti=str(values["token_jti"]),
                token_expiration=datetime.fromisoformat(
                    values["token_expiration"]
                ),
            )
            if claims.token_expiration <= utc_now():
                raise AuthenticationError(
                    "Access Token已过期",
                    error_code="INVALID_TOKEN",
                )
            return claims
        except AuthenticationError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthenticationError(
                "WebSocket Ticket内容无效",
                error_code="WS_TICKET_INVALID",
            ) from exc
