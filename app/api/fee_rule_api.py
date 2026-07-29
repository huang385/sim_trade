from datetime import date

from fastapi import (
    APIRouter,
    Depends,
    Query,
)
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_admin_user
from app.schemas.fee_rule_schema import (
    FeeRuleCreate,
    FeeRuleDailyCreate,
    FeeRuleDailyResponse,
    FeeRuleResponse,
)
from app.services.fee_rule_service import (
    FeeRuleService,
    get_fee_rule_service,
)


router = APIRouter(
    prefix="/api/admin/fee-rules",
    tags=["手续费规则管理"],
    dependencies=[Depends(require_admin_user)],
)


@router.put(
    "/current",
    response_model=FeeRuleResponse,
)
def upsert_current_fee_rule(
    request: FeeRuleCreate,
    db: Session = Depends(get_db),
    service: FeeRuleService = Depends(
        get_fee_rule_service
    ),
):
    """
    人工创建或更新当前手续费规则。
    """

    return service.upsert_current_manual(
        db=db,
        request=request,
    )


@router.get(
    "/current/{exchange_id}/{symbol}",
    response_model=FeeRuleResponse,
)
def get_current_fee_rule(
    exchange_id: str,
    symbol: str,
    db: Session = Depends(get_db),
    service: FeeRuleService = Depends(
        get_fee_rule_service
    ),
):
    """
    查询某合约当前手续费规则。
    """

    return service.get_current(
        db=db,
        exchange_id=exchange_id,
        symbol=symbol,
    )


@router.get(
    "/current",
    response_model=list[FeeRuleResponse],
)
def list_current_fee_rules(
    exchange_id: str | None = Query(
        default=None,
    ),
    db: Session = Depends(get_db),
    service: FeeRuleService = Depends(
        get_fee_rule_service
    ),
):
    """
    查询当前手续费规则列表。
    """

    return service.list_current(
        db=db,
        exchange_id=exchange_id,
    )


@router.put(
    "/daily",
    response_model=FeeRuleDailyResponse,
)
def upsert_daily_fee_rule(
    request: FeeRuleDailyCreate,
    db: Session = Depends(get_db),
    service: FeeRuleService = Depends(
        get_fee_rule_service
    ),
):
    """
    人工创建或更新某个交易日的手续费规则。
    """

    return service.upsert_daily_manual(
        db=db,
        request=request,
    )


@router.get(
    "/daily/{trading_day}/{exchange_id}/{symbol}",
    response_model=FeeRuleDailyResponse,
)
def get_daily_fee_rule(
    trading_day: date,
    exchange_id: str,
    symbol: str,
    db: Session = Depends(get_db),
    service: FeeRuleService = Depends(
        get_fee_rule_service
    ),
):
    """
    查询某交易日、某合约手续费规则。
    """

    return service.get_daily(
        db=db,
        trading_day=trading_day,
        exchange_id=exchange_id,
        symbol=symbol,
    )


@router.get(
    "/daily/{trading_day}",
    response_model=list[FeeRuleDailyResponse],
)
def list_daily_fee_rules(
    trading_day: date,
    exchange_id: str | None = Query(
        default=None,
    ),
    db: Session = Depends(get_db),
    service: FeeRuleService = Depends(
        get_fee_rule_service
    ),
):
    """
    查询指定交易日的手续费规则列表。
    """

    return service.list_daily(
        db=db,
        trading_day=trading_day,
        exchange_id=exchange_id,
    )
