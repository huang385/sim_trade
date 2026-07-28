from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.redis_client import redis_client
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.schemas.pnl_schema import (
    AccountRealtimePnlResponse,
    PositionRealtimePnlResponse,
)
from app.services.realtime_pnl_query_service import RealtimePnlQueryService


router = APIRouter(tags=["实时盈亏"])


def get_realtime_pnl_query_service() -> RealtimePnlQueryService:
    """构造只读查询服务；Redis连接由应用级客户端统一复用。"""

    return RealtimePnlQueryService(
        pnl_store=RealtimePnlStore(redis_client)
    )


@router.get(
    "/api/accounts/{account_id}/pnl/realtime",
    response_model=AccountRealtimePnlResponse,
)
def get_account_realtime_pnl(
    account_id: str,
    db: Session = Depends(get_db),
    service: RealtimePnlQueryService = Depends(
        get_realtime_pnl_query_service
    ),
):
    """查询账户盘中实时盈亏，Redis无快照时返回PostgreSQL持久化结果。"""

    return service.get_account(db, account_id)


@router.get(
    "/api/positions/{position_id}/pnl/realtime",
    response_model=PositionRealtimePnlResponse,
)
def get_position_realtime_pnl(
    position_id: str,
    db: Session = Depends(get_db),
    service: RealtimePnlQueryService = Depends(
        get_realtime_pnl_query_service
    ),
):
    """查询单个持仓盘中实时盈亏，响应会明确标记实际数据来源。"""

    return service.get_position(db, position_id)
