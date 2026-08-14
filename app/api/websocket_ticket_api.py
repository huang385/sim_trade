from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials

from app.core.redis_client import redis_client
from app.core.security import (
    bearer_scheme,
    get_current_user,
    get_token_service,
)
from app.enums.auth_enums import TokenType
from app.models.app_user import AppUser
from app.realtime.metrics import realtime_metrics
from app.modules.realtime import WebSocketTicketService
from app.schemas.websocket_schema import WebSocketTicketResponse
from app.modules.auth import TokenService


router = APIRouter(prefix="/api/ws", tags=["WebSocket实时推送"])
_ticket_service = WebSocketTicketService(redis_client)


def get_websocket_ticket_service() -> WebSocketTicketService:
    return _ticket_service


@router.post("/ticket", response_model=WebSocketTicketResponse)
def create_websocket_ticket(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    current_user: AppUser = Depends(get_current_user),
    token_service: TokenService = Depends(get_token_service),
    service: WebSocketTicketService = Depends(
        get_websocket_ticket_service
    ),
):
    """为当前Access Token签发30秒左右有效的一次性连接票据。"""

    # get_current_user已经验证用户状态；这里再次解析同一凭证只为取得已验证
    # jti和到期时间，Ticket不会保存原始Access Token。
    claims = token_service.decode(
        credentials.credentials,
        expected_type=TokenType.ACCESS,
    )
    issued = service.create(
        user_id=current_user.user_id,
        role=current_user.role,
        token_jti=claims.jti,
        token_expiration=claims.expires_at,
    )
    realtime_metrics.increment("ws_ticket_created")
    return WebSocketTicketResponse(
        ticket=issued.ticket,
        expires_in=issued.expires_in,
    )
