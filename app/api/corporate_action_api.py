"""Corporate-action administration and entitled-user endpoints."""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.exceptions import ResourceNotFoundError
from app.core.database import get_db
from app.core.security import require_active_user, require_admin_user
from app.models.app_user import AppUser
from app.models.cash_security_corporate_action import CashSecurityCorporateAction
from app.models.cash_security_corporate_action_entitlement import CashSecurityCorporateActionEntitlement
from app.models.account import Account
from app.schemas.corporate_action_schema import (
    CorporateActionEntitlementResponse,
    CorporateActionImportRequest,
    CorporateActionResponse,
    PriceAdjustmentFactorCreate,
    RightsSubscriptionRequest,
)
from app.services.account_access_scope import AccountAccessScope
from app.services.cash_security_corporate_action_service import CashSecurityCorporateActionService


admin_router = APIRouter(
    prefix="/api/admin/corporate-actions", tags=["公司行为管理"], dependencies=[Depends(require_admin_user)]
)
router = APIRouter(prefix="/api/corporate-actions", tags=["公司行为"])


def _service() -> CashSecurityCorporateActionService:
    return CashSecurityCorporateActionService()


@admin_router.post("", response_model=CorporateActionResponse)
def import_corporate_action(request: CorporateActionImportRequest, db: Session = Depends(get_db)):
    """仅管理端导入公告事实；导入本身不会立即向任何账户派发资产。"""
    service = _service()
    action = service.import_action(db, payload=request.model_dump(exclude={"components"}), components=[row.model_dump() for row in request.components])
    db.commit()
    db.refresh(action)
    return action


@admin_router.post("/{action_id}/confirm", response_model=CorporateActionResponse)
def confirm_corporate_action(action_id: str, db: Session = Depends(get_db)):
    service = _service()
    service.confirm(db, action_id=action_id)
    db.commit()
    action = db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id))
    return action


@admin_router.post("/{action_id}/entitlements/{trading_day}", response_model=CorporateActionResponse)
def capture_entitlements(action_id: str, trading_day: date, db: Session = Depends(get_db)):
    service = _service()
    service.capture_entitlements(db, action_id=action_id, trading_day=trading_day)
    db.commit()
    return db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id))


@admin_router.post("/{action_id}/ex-date/{trading_day}", response_model=CorporateActionResponse)
def apply_ex_date(action_id: str, trading_day: date, db: Session = Depends(get_db)):
    service = _service()
    service.apply_ex_date(db, action_id=action_id, trading_day=trading_day)
    db.commit()
    return db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id))


@admin_router.post("/{action_id}/payment/{trading_day}", response_model=CorporateActionResponse)
def pay_corporate_action(action_id: str, trading_day: date, db: Session = Depends(get_db)):
    service = _service()
    service.pay_cash(db, action_id=action_id, trading_day=trading_day)
    db.commit()
    return db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id))


@admin_router.post("/{action_id}/listing/{trading_day}", response_model=CorporateActionResponse)
def list_corporate_action_shares(action_id: str, trading_day: date, db: Session = Depends(get_db)):
    service = _service()
    service.list_pending_shares(db, action_id=action_id, trading_day=trading_day)
    db.commit()
    return db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id))


@admin_router.post("/{action_id}/maturity/{trading_day}", response_model=CorporateActionResponse)
def apply_bond_maturity(action_id: str, trading_day: date, db: Session = Depends(get_db)):
    service = _service()
    service.apply_bond_maturity(db, action_id=action_id, trading_day=trading_day)
    db.commit()
    return db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id))


@admin_router.post("/{action_id}/price-factor", response_model=CorporateActionResponse)
def record_price_factor(action_id: str, request: PriceAdjustmentFactorCreate, db: Session = Depends(get_db)):
    _service().record_price_adjustment_factor(db, action_id=action_id, **request.model_dump())
    db.commit()
    return db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id))


@router.post("/{action_id}/subscribe", response_model=CorporateActionEntitlementResponse)
def subscribe_rights(action_id: str, request: RightsSubscriptionRequest, current_user: AppUser = Depends(require_active_user), db: Session = Depends(get_db)):
    """配股是唯一由客户主动触发的公司行为；资格仍来自登记日权益快照。"""
    account = db.scalar(select(Account).where(Account.account_id == request.account_id))
    trading_day = account.trading_day if account is not None and account.trading_day is not None else date.today()
    entitlement = _service().subscribe_rights(db, action_id=action_id, account_id=request.account_id, volume=request.volume, client_request_id=request.client_request_id, access_scope=AccountAccessScope.from_current_user(current_user), trading_day=trading_day)
    db.commit()
    db.refresh(entitlement)
    return entitlement


@router.get("/{action_id}/entitlements", response_model=list[CorporateActionEntitlementResponse])
def list_my_entitlements(action_id: str, account_id: str = Query(min_length=1), current_user: AppUser = Depends(require_active_user), db: Session = Depends(get_db)):
    scope = AccountAccessScope.from_current_user(current_user)
    query = select(CashSecurityCorporateActionEntitlement).where(CashSecurityCorporateActionEntitlement.action_id == action_id, CashSecurityCorporateActionEntitlement.account_id == account_id).order_by(CashSecurityCorporateActionEntitlement.id)
    rows = db.scalars(query).all()
    if not scope.is_admin:
        # Verify ownership before returning an empty idempotent-looking list.
        from app.models.account import Account
        account = db.scalar(select(Account).where(Account.account_id == account_id))
        if account is None or account.user_id != scope.user_id:
            raise ResourceNotFoundError("账户不存在", error_code="ACCOUNT_NOT_FOUND")
    return rows
