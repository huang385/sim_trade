from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_active_user, require_admin_user
from app.models.app_user import AppUser
from app.schemas.account_schema import (
    AccountCreate,
    AccountResponse,
)
from app.services.account_service import (
    AccountService,
    get_account_service,
)
from app.services.account_authorization_service import (
    AccountAuthorizationService,
)
from app.api.auth_api import get_account_authorization_service


router = APIRouter(
    prefix="/api/accounts",
    tags=["账户管理"],
)


@router.post(
    "",
    response_model=AccountResponse,
)
def create_account(
    request: AccountCreate,
    _admin: AppUser = Depends(require_admin_user),
    db: Session = Depends(get_db),
    service: AccountService = Depends(
        get_account_service
    ),
):
    """
    创建模拟交易账户。
    """

    return service.create_account(
        db=db,
        request=request,
    )


@router.get(
    "/{account_id}",
    response_model=AccountResponse,
)
def get_account(
    account_id: str,
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db),
):
    """
    查询账户。
    """

    # 授权服务返回已加载账户，避免随后再次查询同一账户。
    return authorization.require_account_access(
        db, current_user, account_id
    )


@router.get(
    "",
    response_model=list[AccountResponse],
)
def list_accounts(
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db),
):
    """
    查询全部账户。
    """

    return authorization.list_accessible_accounts(db, current_user)
