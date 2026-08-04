from sqlalchemy.orm import Session

from app.common.exceptions import AuthenticationError
from app.enums.auth_enums import UserStatus
from app.models.app_user import AppUser
from app.repositories.user_repository import UserRepository
from app.realtime.websocket_ticket_service import WebSocketTicketClaims


class WebSocketAuthService:
    """消费Ticket后再次以PostgreSQL事实校验用户当前状态。"""

    def __init__(self, repository: UserRepository | None = None):
        self.repository = repository or UserRepository()

    def authenticate(
        self,
        db: Session,
        claims: WebSocketTicketClaims,
    ) -> AppUser:
        user = self.repository.get_by_user_id(db, claims.user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AuthenticationError(
                "当前用户不可用",
                error_code="USER_INACTIVE",
            )
        return user

    def is_active(self, db: Session, user_id: str) -> bool:
        user = self.repository.get_by_user_id(db, user_id)
        return bool(user and user.status == UserStatus.ACTIVE.value)
