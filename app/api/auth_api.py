from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.app_user import AppUser
from app.schemas.auth_schema import (
    CurrentUserResponse,
    LoginRequest,
    TokenResponse,
)
from app.schemas.user_schema import UserSummary
from app.modules.accounts import (
    AccountAuthorizationService,
)
from app.modules.auth import (
    AuthResult,
    AuthService,
    get_account_authorization_service,
    get_auth_service,
)


router = APIRouter(prefix="/api/auth", tags=["用户认证"])

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
