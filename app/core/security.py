from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.common.exceptions import (
    AuthenticationError,
    AuthorizationError,
)
from app.core.database import get_db
from app.enums.auth_enums import TokenType, UserRole, UserStatus
from app.models.app_user import AppUser
from app.repositories.user_repository import UserRepository
from app.services.token_service import TokenService


bearer_scheme = HTTPBearer(auto_error=False)
_token_service = TokenService()
_user_repository = UserRepository()


def get_token_service() -> TokenService:
    return _token_service


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        bearer_scheme
    ),
    db: Session = Depends(get_db, scope="function"),
    token_service: TokenService = Depends(get_token_service),
) -> AppUser:
    """
    每个请求只解析一次Access Token并查询一次用户。

    FastAPI会在同一请求内缓存依赖结果，后续账户授权直接复用该ORM对象。
    """

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError(
            "请先登录",
            error_code="AUTHENTICATION_REQUIRED",
        )
    claims = token_service.decode(
        credentials.credentials,
        expected_type=TokenType.ACCESS,
    )
    user = _user_repository.get_by_user_id(db, claims.user_id)
    if user is None or user.status != UserStatus.ACTIVE.value:
        raise AuthenticationError(
            "当前用户不可用",
            error_code="USER_INACTIVE",
        )
    return user


def require_active_user(
    current_user: AppUser = Depends(get_current_user),
) -> AppUser:
    return current_user


def require_admin_user(
    current_user: AppUser = Depends(get_current_user),
) -> AppUser:
    if current_user.role != UserRole.ADMIN.value:
        raise AuthorizationError(
            "该操作仅限管理员",
            error_code="ADMIN_REQUIRED",
        )
    return current_user
