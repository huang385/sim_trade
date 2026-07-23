from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.position_schema import PositionResponse
from app.services.trade_settlement_service import PositionQueryService


router = APIRouter(prefix="/api/positions", tags=["持仓查询"])


def get_position_query_service() -> PositionQueryService:
    return PositionQueryService()


@router.get("", response_model=list[PositionResponse])
def list_positions(
    account_id: str = Query(min_length=1, max_length=64),
    db: Session = Depends(get_db),
    service: PositionQueryService = Depends(get_position_query_service),
):
    """查询指定账户的多空持仓汇总。"""

    return service.list(db, account_id)
