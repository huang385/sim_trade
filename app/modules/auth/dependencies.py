"""认证模块装配入口；API 只依赖这些公共工厂。"""

from app.core.redis_client import redis_client
from app.repositories.auth_refresh_session_repository import (
    AuthRefreshSessionRepository,
)
from app.repositories.user_repository import UserRepository
from app.services.account_authorization_service import AccountAuthorizationService
from app.services.admin_user_service import AdminUserService
from app.services.auth_service import AuthService
from app.services.login_rate_limit_service import LoginRateLimitService
from app.services.password_service import PasswordService
from app.services.token_service import TokenService

_password_service = PasswordService()
_token_service = TokenService()
_auth_service = AuthService(
    user_repository=UserRepository(),
    refresh_repository=AuthRefreshSessionRepository(),
    password_service=_password_service,
    token_service=_token_service,
    rate_limit_service=LoginRateLimitService(redis_client),
)
_authorization_service = AccountAuthorizationService()
_admin_user_service = AdminUserService(
    repository=UserRepository(),
    password_service=_password_service,
)


def get_auth_service() -> AuthService:
    return _auth_service


def get_account_authorization_service() -> AccountAuthorizationService:
    return _authorization_service


def get_admin_user_service() -> AdminUserService:
    return _admin_user_service


__all__ = [
    "get_account_authorization_service",
    "get_admin_user_service",
    "get_auth_service",
]
