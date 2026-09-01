from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_active_user
from app.models.app_user import AppUser
from app.repositories.risk_repository import RiskRepository
from app.schemas.risk_schema import (
    LiquidationTaskResponse,
    RiskEventResponse,
    RiskSnapshotResponse,
)
from app.services.account_authorization_service import (
    AccountAuthorizationService,
    get_account_authorization_service,
)


router = APIRouter(prefix="/api/risk", tags=["实时风险"])


@router.get("/accounts/{account_id}", response_model=RiskSnapshotResponse)
def get_risk_snapshot(
    account_id: str,
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db, scope="function"),
):
    """返回当前用户有权访问的账户风险状态和最近强平任务。"""

    account = authorization.require_account_access(db, current_user, account_id)
    tasks = RiskRepository.list_tasks_by_account(db, account_id, limit=1)
    return RiskSnapshotResponse(
        account_id=account.account_id,
        risk_state=account.risk_state,
        risk_version=account.risk_version,
        risk_ratio=account.risk_ratio,
        equity=account.equity,
        available_cash=account.available_cash,
        risk_available_cash=account.risk_available_cash,
        latest_task=tasks[0] if tasks else None,
    )


@router.get(
    "/accounts/{account_id}/events", response_model=list[RiskEventResponse]
)
def list_risk_events(
    account_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db, scope="function"),
):
    """分页上限内读取账户风险审计记录，不暴露其他用户账户。"""

    authorization.require_account_access(db, current_user, account_id)
    return RiskRepository.list_events_by_account(db, account_id, limit=limit)


@router.get(
    "/accounts/{account_id}/liquidations",
    response_model=list[LiquidationTaskResponse],
)
def list_liquidation_tasks(
    account_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db, scope="function"),
):
    """读取强平任务进度；账户转移或权限撤销后立即停止访问。"""

    authorization.require_account_access(db, current_user, account_id)
    return RiskRepository.list_tasks_by_account(db, account_id, limit=limit)
