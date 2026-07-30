from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_active_user
from app.enums.auth_enums import UserRole
from app.models.app_user import AppUser
from app.api.auth_api import get_account_authorization_service
from app.services.account_authorization_service import (
    AccountAuthorizationService,
)
from app.schemas.order_schema import (
    OrderCancelRequest,
    OrderCreateRequest,
    OrderPageResponse,
    OrderResponse,
)
from app.services.order_cancellation_service import (
    OrderCancellationService,
    get_order_cancellation_service,
)
from app.services.order_service import OrderService, get_order_service


router = APIRouter(
    # 订单接口与后台参考数据管理接口分开，供交易客户端调用。
    prefix="/api/orders",
    tags=["订单管理"],
)


@router.post("", response_model=OrderResponse)
def create_order(
    request: OrderCreateRequest,
    current_user: AppUser = Depends(require_active_user),
    db: Session = Depends(get_db),
    service: OrderService = Depends(get_order_service),
):
    """
    接收限价开仓订单。

    API 层只负责接收和返回数据，具体的规则查询、金额计算、
    账户锁定、资金冻结以及事务提交全部由 OrderService 处理。

    相同账户和 client_order_id 重复提交时返回原订单。
    """

    return service.create_order(
        db=db,
        request=request,
        account_owner_user_id=(
            None
            if current_user.role == UserRole.ADMIN.value
            else current_user.user_id
        ),
        conceal_account_existence=(
            current_user.role != UserRole.ADMIN.value
        ),
    )


@router.get("/page", response_model=OrderPageResponse)
def list_order_page(
    account_id: str = Query(min_length=1, max_length=64),
    cursor: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db),
    service: OrderService = Depends(get_order_service),
):
    """
    按数据库主键倒序返回订单分页。

    旧的GET /api/orders列表接口继续保留；新客户端使用本接口响应中的
    next_cursor继续翻页，不需要知道数据库内部主键。
    """

    authorization.require_account_access(db, current_user, account_id)
    return service.list_order_page(
        db,
        account_id,
        cursor=cursor,
        limit=limit,
    )


@router.post("/{order_id}/cancel", response_model=OrderResponse)
def cancel_order(
    order_id: str,
    request: OrderCancelRequest,
    current_user: AppUser = Depends(require_active_user),
    db: Session = Depends(get_db),
    service: OrderCancellationService = Depends(
        get_order_cancellation_service
    ),
):
    """
    撤销限价开仓订单的全部剩余未成交数量。

    API 层不修改订单、账户或 Redis；资金释放、状态更新、Outbox 写入和
    PostgreSQL 事务全部由 OrderCancellationService 负责。
    """

    return service.cancel_order(
        db=db,
        order_id=order_id,
        request=request,
        account_owner_user_id=(
            None
            if current_user.role == UserRole.ADMIN.value
            else current_user.user_id
        ),
        conceal_resource_existence=(
            current_user.role != UserRole.ADMIN.value
        ),
    )


@router.get("/{order_id}", response_model=OrderResponse)
def get_order(
    order_id: str,
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db),
):
    """
    按系统订单编号查询订单。

    order_id 是服务端生成的编号，不是客户端提供的
    client_order_id。
    """

    return authorization.require_order_access(
        db,
        current_user,
        order_id,
    )


@router.get("", response_model=list[OrderResponse])
def list_orders(
    account_id: str = Query(min_length=1, max_length=64),
    after_id: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db),
    service: OrderService = Depends(get_order_service),
):
    """
    查询指定账户的订单列表。

    第一阶段数据量较小，暂不分页；后续订单量增大后应增加
    时间范围、状态过滤和游标分页。
    """

    authorization.require_account_access(db, current_user, account_id)
    return service.list_orders(
        db=db,
        account_id=account_id,
        after_id=after_id,
        limit=limit,
    )
