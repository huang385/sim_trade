from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.order_schema import OrderCreateRequest, OrderResponse
from app.services.order_service import OrderService, get_order_service


router = APIRouter(
    # 订单接口与后台参考数据管理接口分开，供交易客户端调用。
    prefix="/api/orders",
    tags=["订单管理"],
)


@router.post("", response_model=OrderResponse)
def create_order(
    request: OrderCreateRequest,
    db: Session = Depends(get_db),
    service: OrderService = Depends(get_order_service),
):
    """
    接收限价开仓订单。

    API 层只负责接收和返回数据，具体的规则查询、金额计算、
    账户锁定、资金冻结以及事务提交全部由 OrderService 处理。

    相同账户和 client_order_id 重复提交时返回原订单。
    """

    return service.create_order(db=db, request=request)


@router.get("/{order_id}", response_model=OrderResponse)
def get_order(
    order_id: str,
    db: Session = Depends(get_db),
    service: OrderService = Depends(get_order_service),
):
    """
    按系统订单编号查询订单。

    order_id 是服务端生成的编号，不是客户端提供的
    client_order_id。
    """

    return service.get_order(db=db, order_id=order_id)


@router.get("", response_model=list[OrderResponse])
def list_orders(
    account_id: str = Query(min_length=1, max_length=64),
    db: Session = Depends(get_db),
    service: OrderService = Depends(get_order_service),
):
    """
    查询指定账户的订单列表。

    第一阶段数据量较小，暂不分页；后续订单量增大后应增加
    时间范围、状态过滤和游标分页。
    """

    return service.list_orders(db=db, account_id=account_id)
