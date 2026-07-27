from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.trade_schema import (
    TradePositionAllocationResponse,
    TradeResponse,
)
from app.services.trade_settlement_service import TradeQueryService


router = APIRouter(prefix="/api/trades", tags=["成交查询"])


def get_trade_query_service() -> TradeQueryService:
    return TradeQueryService()


@router.get("/{trade_id}", response_model=TradeResponse)
def get_trade(
    trade_id: str,
    db: Session = Depends(get_db),
    service: TradeQueryService = Depends(get_trade_query_service),
):
    """按系统成交编号查询一条成交。"""

    return service.get(db, trade_id)


@router.get(
    "/{trade_id}/position-allocations",
    response_model=list[TradePositionAllocationResponse],
)
def list_trade_position_allocations(
    trade_id: str,
    db: Session = Depends(get_db),
    service: TradeQueryService = Depends(get_trade_query_service),
):
    """查询平仓 Trade 实际关闭的逐笔持仓、保证金、手续费和盈亏。"""

    return service.list_position_allocations(db, trade_id)


@router.get("", response_model=list[TradeResponse])
def list_trades(
    account_id: str | None = Query(default=None, min_length=1, max_length=64),
    order_id: str | None = Query(default=None, min_length=1, max_length=64),
    db: Session = Depends(get_db),
    service: TradeQueryService = Depends(get_trade_query_service),
):
    """按账户或订单查询成交；当前数据量较小，后续可增加游标分页。"""

    return service.list(db, account_id=account_id, order_id=order_id)
