from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_active_user
from app.models.app_user import AppUser
from app.enums.auth_enums import UserRole
from app.common.exceptions import AuthorizationError
from app.api.auth_api import get_account_authorization_service
from app.services.account_authorization_service import (
    AccountAuthorizationService,
)
from app.schemas.trade_schema import (
    TradePageResponse,
    TradePositionAllocationResponse,
    TradeResponse,
)
from app.services.trade_settlement_service import TradeQueryService


router = APIRouter(prefix="/api/trades", tags=["成交查询"])


def get_trade_query_service() -> TradeQueryService:
    return TradeQueryService()


def _authorize_trade_scope(
    *,
    db: Session,
    current_user: AppUser,
    authorization: AccountAuthorizationService,
    account_id: str | None,
    order_id: str | None,
) -> None:
    """把成交列表过滤条件转换为明确的账户授权范围。"""

    if account_id:
        authorization.require_account_access(
            db, current_user, account_id
        )
        return
    if order_id:
        authorization.require_order_access(
            db,
            current_user,
            order_id,
        )
        return
    if current_user.role != UserRole.ADMIN.value:
        raise AuthorizationError(
            "普通用户查询成交时必须指定授权账户",
            error_code="ACCOUNT_SCOPE_REQUIRED",
        )


@router.get("/page", response_model=TradePageResponse)
def list_trade_page(
    account_id: str | None = Query(default=None, min_length=1, max_length=64),
    order_id: str | None = Query(default=None, min_length=1, max_length=64),
    cursor: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db),
    service: TradeQueryService = Depends(get_trade_query_service),
):
    """返回可实际继续翻页的成交游标协议；旧列表接口保持兼容。"""

    _authorize_trade_scope(
        db=db,
        current_user=current_user,
        authorization=authorization,
        account_id=account_id,
        order_id=order_id,
    )
    return service.list_page(
        db,
        account_id=account_id,
        order_id=order_id,
        cursor=cursor,
        limit=limit,
    )


@router.get("/{trade_id}", response_model=TradeResponse)
def get_trade(
    trade_id: str,
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db),
    service: TradeQueryService = Depends(get_trade_query_service),
):
    """按系统成交编号查询一条成交。"""

    return authorization.require_trade_access(
        db,
        current_user,
        trade_id,
    )


@router.get(
    "/{trade_id}/position-allocations",
    response_model=list[TradePositionAllocationResponse],
)
def list_trade_position_allocations(
    trade_id: str,
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db),
    service: TradeQueryService = Depends(get_trade_query_service),
):
    """查询平仓 Trade 实际关闭的逐笔持仓、保证金、手续费和盈亏。"""

    trade = authorization.require_trade_access(
        db,
        current_user,
        trade_id,
    )
    return service.list_position_allocations(
        db, trade_id, trade=trade
    )


@router.get("", response_model=list[TradeResponse])
def list_trades(
    account_id: str | None = Query(default=None, min_length=1, max_length=64),
    order_id: str | None = Query(default=None, min_length=1, max_length=64),
    after_id: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db),
    service: TradeQueryService = Depends(get_trade_query_service),
):
    """按账户或订单查询成交；当前数据量较小，后续可增加游标分页。"""

    _authorize_trade_scope(
        db=db,
        current_user=current_user,
        authorization=authorization,
        account_id=account_id,
        order_id=order_id,
    )
    return service.list(
        db,
        account_id=account_id,
        order_id=order_id,
        after_id=after_id,
        limit=limit,
    )
