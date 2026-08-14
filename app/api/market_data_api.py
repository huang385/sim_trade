from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth_api import get_account_authorization_service
from app.core.database import get_db
from app.core.security import require_active_user
from app.models.app_user import AppUser
from app.schemas.market_subscription_schema import (
    OptionMarketPrepareRequest,
    OptionMarketPrepareResponse,
)
from app.modules.accounts import (
    AccountAuthorizationService,
)
from app.modules.market_data import (
    OptionMarketPreSubscriptionService,
    get_option_market_pre_subscription_service,
)


router = APIRouter(
    prefix="/api/market-data/subscriptions",
    tags=["行情订阅"],
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
