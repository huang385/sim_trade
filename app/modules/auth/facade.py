"""认证模块稳定公共入口。"""

from app.services.admin_user_service import AdminUserService
from app.services.auth_service import AuthResult, AuthService
from app.services.login_rate_limit_service import LoginRateLimitService
from app.services.password_service import PasswordService
from app.services.token_service import TokenService
from app.modules.auth.dependencies import (
    get_account_authorization_service,
    get_admin_user_service,
    get_auth_service,
)

__all__ = [
    "AdminUserService",
    "AuthResult",
    "AuthService",
    "LoginRateLimitService",
    "PasswordService",
    "TokenService",
    "get_account_authorization_service",
    "get_admin_user_service",
    "get_auth_service",
]
