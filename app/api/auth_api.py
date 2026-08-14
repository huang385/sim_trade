from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.redis_client import redis_client
from app.core.security import get_current_user
from app.models.app_user import AppUser
from app.repositories.auth_refresh_session_repository import (
    AuthRefreshSessionRepository,
)
from app.repositories.user_repository import UserRepository
from app.schemas.auth_schema import (
    CurrentUserResponse,
    LoginRequest,
    TokenResponse,
)
from app.schemas.user_schema import UserSummary
from app.services.account_authorization_service import (
    AccountAuthorizationService,
    get_account_authorization_service,
)
from app.services.auth_service import AuthResult, AuthService
from app.services.login_rate_limit_service import LoginRateLimitService
from app.services.password_service import PasswordService
from app.services.token_service import TokenService


router = APIRouter(prefix="/api/auth", tags=["用户认证"])

_password_service = PasswordService()
_token_service = TokenService()
_auth_service = AuthService(
    user_repository=UserRepository(),
    refresh_repository=AuthRefreshSessionRepository(),
    password_service=_password_service,
    token_service=_token_service,
    rate_limit_service=LoginRateLimitService(redis_client),
)


def get_auth_service() -> AuthService:
    return _auth_service


def _client_ip(request: Request) -> str:
    """仅使用服务端连接信息，不信任客户端自行提交的身份字段。"""

    return request.client.host if request.client else "unknown"


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    response.set_cookie(
        key=settings.auth_refresh_cookie_name,
        value=refresh_token,
        max_age=settings.auth_refresh_token_expire_days * 86400,
        httponly=True,
        secure=settings.auth_refresh_cookie_secure,
        samesite=settings.auth_refresh_cookie_samesite,
        path="/api/auth",
    )


def _token_response(result: AuthResult) -> TokenResponse:
    return TokenResponse(
        access_token=result.tokens.access_token,
        expires_in=result.tokens.access_expires_in,
        user=UserSummary.model_validate(result.user),
    )


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    service: AuthService = Depends(get_auth_service),
):
    result = service.login(
        db,
        username=payload.username,
        password=payload.password,
        client_ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_refresh_cookie(response, result.tokens.refresh_token)
    return _token_response(result)


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    service: AuthService = Depends(get_auth_service),
):
    token = request.cookies.get(settings.auth_refresh_cookie_name, "")
    result = service.refresh(
        db,
        refresh_token=token,
        client_ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_refresh_cookie(response, result.tokens.refresh_token)
    return _token_response(result)


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    service: AuthService = Depends(get_auth_service),
):
    service.logout(
        db,
        refresh_token=request.cookies.get(
            settings.auth_refresh_cookie_name
        ),
    )
    response.delete_cookie(
        settings.auth_refresh_cookie_name,
        path="/api/auth",
    )


@router.get("/me", response_model=CurrentUserResponse)
def me(
    current_user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
):
    return CurrentUserResponse(
        user=UserSummary.model_validate(current_user),
        accounts=list(
            authorization.list_accessible_accounts(db, current_user)
        ),
    )
