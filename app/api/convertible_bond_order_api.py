"""HTTP entry points for convertible-bond cash orders."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_active_user
from app.models.app_user import AppUser
from app.schemas.order_schema import (
    ConvertibleBondOrderCreateRequest,
    OrderCancelRequest,
    OrderResponse,
)
from app.services.account_access_scope import AccountAccessScope
from app.services.convertible_bond_order_service import (
    ConvertibleBondOrderCancellationService,
    ConvertibleBondOrderService,
    get_convertible_bond_order_cancellation_service,
    get_convertible_bond_order_service,
)


router = APIRouter(prefix="/api/convertible-bond/orders", tags=["可转债订单"])


@router.post("", response_model=OrderResponse)
def create_convertible_bond_order(
    request: ConvertibleBondOrderCreateRequest,
    current_user: AppUser = Depends(require_active_user),
    db: Session = Depends(get_db),
    service: ConvertibleBondOrderService = Depends(get_convertible_bond_order_service),
):
    return service.create_order(
        db=db,
        request=request,
        access_scope=AccountAccessScope.from_current_user(current_user),
    )


@router.post("/{order_id}/cancel", response_model=OrderResponse)
def cancel_convertible_bond_order(
    order_id: str,
    request: OrderCancelRequest,
    current_user: AppUser = Depends(require_active_user),
    db: Session = Depends(get_db),
    service: ConvertibleBondOrderCancellationService = Depends(
        get_convertible_bond_order_cancellation_service
    ),
):
    return service.cancel_order(
        db=db,
        order_id=order_id,
        request=request,
        access_scope=AccountAccessScope.from_current_user(current_user),
    )
