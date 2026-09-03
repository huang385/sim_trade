"""HTTP entry points for ETF secondary-market cash orders."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_active_user
from app.models.app_user import AppUser
from app.schemas.order_schema import (
    EtfOrderCreateRequest,
    OrderCancelRequest,
    OrderFeeComponentSnapshotResponse,
    OrderResponse,
)
from app.repositories.order_fee_component_snapshot_repository import (
    OrderFeeComponentSnapshotRepository,
)
from app.services.account_authorization_service import (
    AccountAuthorizationService,
    get_account_authorization_service,
)
from app.services.account_access_scope import AccountAccessScope
from app.services.etf_order_service import (
    EtfOrderCancellationService,
    EtfOrderService,
    get_etf_order_cancellation_service,
    get_etf_order_service,
)


router = APIRouter(prefix="/api/etf/orders", tags=["ETF订单"])


@router.post("", response_model=OrderResponse)
def create_etf_order(
    request: EtfOrderCreateRequest,
    current_user: AppUser = Depends(require_active_user),
    db: Session = Depends(get_db, scope="function"),
    service: EtfOrderService = Depends(get_etf_order_service),
):
    return service.create_order(
        db=db,
        request=request,
        access_scope=AccountAccessScope.from_current_user(current_user),
    )


@router.post("/{order_id}/cancel", response_model=OrderResponse)
def cancel_etf_order(
    order_id: str,
    request: OrderCancelRequest,
    current_user: AppUser = Depends(require_active_user),
    db: Session = Depends(get_db, scope="function"),
    service: EtfOrderCancellationService = Depends(
        get_etf_order_cancellation_service
    ),
):
    return service.cancel_order(
        db=db,
        order_id=order_id,
        request=request,
        access_scope=AccountAccessScope.from_current_user(current_user),
    )


@router.get(
    "/{order_id}/fee-components",
    response_model=list[OrderFeeComponentSnapshotResponse],
)
def list_etf_order_fee_components(
    order_id: str,
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db, scope="function"),
):
    order = authorization.require_order_access(db, current_user, order_id)
    if order.instrument_type != "ETF":
        return []
    return OrderFeeComponentSnapshotRepository().list_by_order_ids(
        db, [order.order_id]
    ).get(order.order_id, [])
