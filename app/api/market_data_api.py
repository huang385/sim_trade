from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_active_user
from app.models.app_user import AppUser
from app.schemas.market_subscription_schema import (
    OptionMarketPrepareRequest,
    OptionMarketPrepareResponse,
)
from app.services.account_authorization_service import (
    AccountAuthorizationService,
    get_account_authorization_service,
)
from app.services.option_market_pre_subscription_service import (
    OptionMarketPreSubscriptionService,
    get_option_market_pre_subscription_service,
)
from app.infrastructure.market_data.historical_price_client import YmmHistoricalPriceClient
from app.schemas.historical_price_schema import HistoricalPriceBarResponse
from app.services.cash_security_historical_price_query_service import (
    CashSecurityHistoricalPriceQueryService,
)


router = APIRouter(
    prefix="/api/market-data/subscriptions",
    tags=["行情订阅"],
)


@router.get("/history-bars", response_model=list[HistoricalPriceBarResponse])
def get_cash_security_history_bars(
    order_book_id: str = Query(min_length=1, max_length=64),
    start_date: date = Query(),
    end_date: date = Query(),
    adjustment_mode: str = Query(default="RAW", pattern="^(RAW|FORWARD|BACKWARD)$"),
    _: AppUser = Depends(require_active_user),
    db: Session = Depends(get_db),
):
    if end_date < start_date:
        from app.common.exceptions import BusinessRuleError
        raise BusinessRuleError("end_date cannot precede start_date")
    return CashSecurityHistoricalPriceQueryService().query_daily_bars(
        db,
        source=YmmHistoricalPriceClient(),
        order_book_id=order_book_id,
        start_date=start_date,
        end_date=end_date,
        adjustment_mode=adjustment_mode,
    )


@router.post("/prepare", response_model=OptionMarketPrepareResponse)
def prepare_option_market_data(
    request: OptionMarketPrepareRequest,
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db),
    service: OptionMarketPreSubscriptionService = Depends(
        get_option_market_pre_subscription_service
    ),
):
    """为当前用户有权操作的期权账户准备期权及标的实时行情。"""

    account = authorization.require_account_access(
        db,
        current_user,
        request.account_id,
    )
    return service.prepare(db, account=account, request=request)


@router.get("/status", response_model=OptionMarketPrepareResponse)
def get_option_market_data_status(
    account_id: str = Query(min_length=1, max_length=64),
    exchange_id: str = Query(min_length=1, max_length=32),
    symbol: str = Query(min_length=1, max_length=64),
    current_user: AppUser = Depends(require_active_user),
    authorization: AccountAuthorizationService = Depends(
        get_account_authorization_service
    ),
    db: Session = Depends(get_db),
    service: OptionMarketPreSubscriptionService = Depends(
        get_option_market_pre_subscription_service
    ),
):
    """查询期权预订阅是否仍有效，以及期权和标的行情是否均已就绪。"""

    authorization.require_account_access(db, current_user, account_id)
    return service.get_status(
        db,
        account_id=account_id,
        exchange_id=exchange_id,
        symbol=symbol,
    )
