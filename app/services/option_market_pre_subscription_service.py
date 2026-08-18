from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.common.code_utils import normalize_code
from app.common.exceptions import (
    BusinessRuleError,
    ServiceUnavailableError,
)
from app.core.config import settings
from app.core.redis_client import redis_client
from app.enums.account_enums import AccountStatus
from app.enums.instrument_enums import InstrumentType
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.market_pre_subscription_store import (
    MarketPreSubscriptionStore,
)
from app.models.account import Account
from app.models.instrument import Instrument
from app.repositories.instrument_repository import InstrumentRepository
from app.schemas.market_subscription_schema import (
    MarketPreparationStatus,
    OptionMarketPrepareRequest,
    OptionMarketPrepareResponse,
)
from app.services.option_trading_permission_service import (
    OptionTradingPermissionService,
)


OPTION_UNDERLYING_TYPES = {
    InstrumentType.FUTURES_OPTION.value: InstrumentType.FUTURES.value,
    InstrumentType.INDEX_OPTION.value: InstrumentType.INDEX.value,
}


@dataclass(frozen=True)
class OptionSubscriptionContext:
    """一次期权预订阅所需的期权和标的合约事实。"""

    option: Instrument
    underlying: Instrument

    @property
    def requested_codes(self) -> frozenset[str]:
        return frozenset(
            {
                self.option.order_book_id,
                self.underlying.order_book_id,
            }
        )


class OptionMarketPreSubscriptionService:
    """统一准备商品期权和股指期权下单依赖的实时行情。"""

    def __init__(
        self,
        *,
        instrument_repository: InstrumentRepository,
        pre_subscription_store: MarketPreSubscriptionStore,
        market_tick_store: MarketTickStore,
        permission_service: OptionTradingPermissionService,
    ) -> None:
        self.instrument_repository = instrument_repository
        self.pre_subscription_store = pre_subscription_store
        self.market_tick_store = market_tick_store
        self.permission_service = permission_service

    def _load_context(
        self,
        db: Session,
        *,
        exchange_id: str,
        symbol: str,
    ) -> OptionSubscriptionContext:
        exchange_id = normalize_code(exchange_id)
        symbol = normalize_code(symbol)
        option = self.instrument_repository.get(
            db,
            exchange_id=exchange_id,
            symbol=symbol,
        )
        if option is None:
            raise BusinessRuleError(
                "期权合约不存在",
                error_code="OPTION_INSTRUMENT_NOT_FOUND",
            )
        expected_underlying_type = OPTION_UNDERLYING_TYPES.get(
            option.instrument_type
        )
        if expected_underlying_type is None:
            raise BusinessRuleError(
                "行情预订阅接口只支持期权合约",
                error_code="OPTION_PRE_SUBSCRIPTION_ONLY",
            )
        if not option.is_active or not option.is_tradeable:
            raise BusinessRuleError(
                "期权合约当前不可交易",
                error_code="OPTION_INSTRUMENT_NOT_TRADEABLE",
            )
        if option.underlying_instrument_id is None:
            raise BusinessRuleError(
                "期权缺少标的合约",
                error_code="OPTION_UNDERLYING_NOT_FOUND",
            )
        underlying = self.instrument_repository.get_by_id(
            db,
            option.underlying_instrument_id,
        )
        if underlying is None or not underlying.is_active:
            raise BusinessRuleError(
                "期权标的合约不存在或未启用",
                error_code="OPTION_UNDERLYING_NOT_FOUND",
            )
        if underlying.instrument_type != expected_underlying_type:
            raise BusinessRuleError(
                "期权与标的合约类型不匹配",
                error_code="OPTION_UNDERLYING_TYPE_MISMATCH",
            )
        return OptionSubscriptionContext(option=option, underlying=underlying)

    @staticmethod
    def _has_valid_price(values: dict[str, str]) -> bool:
        try:
            price = Decimal(values.get("last_price", ""))
        except (InvalidOperation, ValueError):
            return False
        return price.is_finite() and price > 0

    def _ready_codes(
        self,
        context: OptionSubscriptionContext,
    ) -> list[str]:
        instruments = (context.option, context.underlying)
        snapshots = self.market_tick_store.get_latest_many(
            {
                # 最新行情 Hash 按 order_book_id 建键。此处不能使用内部期权 symbol：
                # 它可能包含分隔符或小写字母，重启恢复后的标准行情缓存将无法命中。
                (instrument.exchange_id, instrument.order_book_id)
                for instrument in instruments
            }
        )
        return sorted(
            instrument.order_book_id
            for instrument in instruments
            if self._has_valid_price(
                snapshots.get(
                    (
                        instrument.exchange_id.strip().upper(),
                        instrument.order_book_id.strip().upper(),
                    ),
                    {},
                )
            )
        )

    @staticmethod
    def _response(
        *,
        account_id: str,
        context: OptionSubscriptionContext,
        expires_at: datetime | None,
        ready_codes: list[str],
        requested: bool,
    ) -> OptionMarketPrepareResponse:
        requested_codes = sorted(context.requested_codes)
        all_ready = set(ready_codes) == set(requested_codes)
        if not requested:
            status = MarketPreparationStatus.NOT_REQUESTED
        elif all_ready:
            status = MarketPreparationStatus.READY
        else:
            status = MarketPreparationStatus.WAITING_MARKET_DATA
        return OptionMarketPrepareResponse(
            account_id=account_id,
            exchange_id=context.option.exchange_id,
            symbol=context.option.symbol,
            status=status,
            requested_codes=requested_codes,
            ready_codes=ready_codes,
            expires_at=expires_at,
            latest_prices_available=all_ready,
        )

    def prepare(
        self,
        db: Session,
        *,
        account: Account,
        request: OptionMarketPrepareRequest,
    ) -> OptionMarketPrepareResponse:
        """校验交易权限后，临时订阅期权及其标的并返回行情就绪状态。"""

        context = self._load_context(
            db,
            exchange_id=request.exchange_id,
            symbol=request.symbol,
        )
        if account.status != AccountStatus.NORMAL.value:
            raise BusinessRuleError(
                "账户当前不可交易",
                error_code="ACCOUNT_NOT_TRADABLE",
            )
        self.permission_service.validate(
            account=account,
            instrument=context.option,
            direction=request.direction,
            offset_flag=request.offset_flag,
        )
        try:
            expires_at = self.pre_subscription_store.request_codes(
                account_id=account.account_id,
                codes=context.requested_codes,
            )
            ready_codes = self._ready_codes(context)
        except RedisError as exc:
            raise ServiceUnavailableError(
                "行情预订阅服务暂不可用",
                error_code="MARKET_PRE_SUBSCRIPTION_UNAVAILABLE",
            ) from exc
        return self._response(
            account_id=account.account_id,
            context=context,
            expires_at=expires_at,
            ready_codes=ready_codes,
            requested=True,
        )

    def get_status(
        self,
        db: Session,
        *,
        account_id: str,
        exchange_id: str,
        symbol: str,
    ) -> OptionMarketPrepareResponse:
        """查询当前账户对指定期权的临时订阅和行情就绪状态。"""

        context = self._load_context(
            db,
            exchange_id=exchange_id,
            symbol=symbol,
        )
        try:
            account_requests = (
                self.pre_subscription_store.get_account_requests(account_id)
            )
            requested_expiries = [
                account_requests[code]
                for code in context.requested_codes
                if code in account_requests
            ]
            requested = len(requested_expiries) == len(
                context.requested_codes
            )
            expires_at = min(requested_expiries) if requested else None
            ready_codes = self._ready_codes(context)
        except RedisError as exc:
            raise ServiceUnavailableError(
                "行情预订阅服务暂不可用",
                error_code="MARKET_PRE_SUBSCRIPTION_UNAVAILABLE",
            ) from exc
        return self._response(
            account_id=account_id,
            context=context,
            expires_at=expires_at,
            ready_codes=ready_codes,
            requested=requested,
        )


def get_option_market_pre_subscription_service(
) -> OptionMarketPreSubscriptionService:
    """构造API使用的通用期权行情预订阅服务。"""

    tick_store = MarketTickStore(redis_client)
    return OptionMarketPreSubscriptionService(
        instrument_repository=InstrumentRepository(),
        pre_subscription_store=MarketPreSubscriptionStore(
            redis_client,
            ttl_seconds=settings.market_pre_subscription_ttl_seconds,
            max_codes_per_account=(
                settings.market_pre_subscription_max_codes_per_account
            ),
        ),
        market_tick_store=tick_store,
        permission_service=OptionTradingPermissionService(),
    )
