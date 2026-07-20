from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.account_schema import (
    AccountCreate,
    AccountResponse,
)
from app.services.account_service import (
    AccountService,
    get_account_service,
)


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
    db: Session = Depends(get_db),
    service: AccountService = Depends(
        get_account_service
    ),
):
    """
    查询账户。
    """

    return service.get_account(
        db=db,
        account_id=account_id,
    )


@router.get(
    "",
    response_model=list[AccountResponse],
)
def list_accounts(
    db: Session = Depends(get_db),
    service: AccountService = Depends(
        get_account_service
    ),
):
    """
    查询全部账户。
    """

    return service.list_accounts(db)