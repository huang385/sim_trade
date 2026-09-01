from datetime import date

from fastapi import (
    APIRouter,
    Depends,
    Query,
)
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_admin_user
from app.schemas.margin_rule_schema import (
    MarginRuleCreate,
    MarginRuleDailyCreate,
    MarginRuleDailyResponse,
    MarginRuleResponse,
)
from app.services.margin_rule_service import (
    MarginRuleService,
    get_margin_rule_service,
)


router = APIRouter(
    prefix="/api/admin/margin-rules",
    tags=["保证金规则管理"],
    dependencies=[Depends(require_admin_user)],
)


@router.put(
    "/current",
    response_model=MarginRuleResponse,
)
def upsert_current_margin_rule(
    request: MarginRuleCreate,
    db: Session = Depends(get_db, scope="function"),
    service: MarginRuleService = Depends(
        get_margin_rule_service
    ),
):
    """
    人工创建或更新当前保证金规则。
    """

    return service.upsert_current_manual(
        db=db,
        request=request,
    )


@router.get(
    "/current/{exchange_id}/{symbol}",
    response_model=MarginRuleResponse,
)
def get_current_margin_rule(
    exchange_id: str,
    symbol: str,
    db: Session = Depends(get_db, scope="function"),
    service: MarginRuleService = Depends(
        get_margin_rule_service
    ),
):
    """
    查询某个合约当前保证金规则。
    """

    return service.get_current(
        db=db,
        exchange_id=exchange_id,
        symbol=symbol,
    )


@router.get(
    "/current",
    response_model=list[MarginRuleResponse],
)
def list_current_margin_rules(
    exchange_id: str | None = Query(
        default=None,
    ),
    db: Session = Depends(get_db, scope="function"),
    service: MarginRuleService = Depends(
        get_margin_rule_service
    ),
):
    """
    查询当前保证金规则列表。
    """

    return service.list_current(
        db=db,
        exchange_id=exchange_id,
    )


@router.put(
    "/daily",
    response_model=MarginRuleDailyResponse,
)
def upsert_daily_margin_rule(
    request: MarginRuleDailyCreate,
    db: Session = Depends(get_db, scope="function"),
    service: MarginRuleService = Depends(
        get_margin_rule_service
    ),
):
    """
    人工创建或更新某个交易日的保证金规则。
    """

    return service.upsert_daily_manual(
        db=db,
        request=request,
    )


@router.get(
    "/daily/{trading_day}/{exchange_id}/{symbol}",
    response_model=MarginRuleDailyResponse,
)
def get_daily_margin_rule(
    trading_day: date,
    exchange_id: str,
    symbol: str,
    db: Session = Depends(get_db, scope="function"),
    service: MarginRuleService = Depends(
        get_margin_rule_service
    ),
):
    """
    查询某交易日、某合约保证金规则。
    """

    return service.get_daily(
        db=db,
        trading_day=trading_day,
        exchange_id=exchange_id,
        symbol=symbol,
    )


@router.get(
    "/daily/{trading_day}",
    response_model=list[MarginRuleDailyResponse],
)
def list_daily_margin_rules(
    trading_day: date,
    exchange_id: str | None = Query(
        default=None,
    ),
    db: Session = Depends(get_db, scope="function"),
    service: MarginRuleService = Depends(
        get_margin_rule_service
    ),
):
    """
    查询指定交易日的保证金规则列表。
    """

    return service.list_daily(
        db=db,
        trading_day=trading_day,
        exchange_id=exchange_id,
    )
