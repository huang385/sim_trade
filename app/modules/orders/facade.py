"""订单应用模块稳定公共入口。"""

from app.modules.orders.matching import (
    DerivativeMatchingStrategy,
    MatchingStrategy,
    MatchingStrategyRegistry,
)
from app.modules.orders.product_registry import (
    FuturesProductStrategy,
    OptionProductStrategy,
    ProductStrategy,
    ProductStrategyRegistry,
    product_strategy_registry,
    resolve_product_strategy,
)
from app.services.order_cancellation_service import (
    OrderCancellationService,
    get_order_cancellation_service,
)
from app.services.order_service import OrderService, get_order_service
from app.services.accepted_order_event_service import (
    AcceptedOrderEventService,
    UnsupportedOrderEventError,
)
from app.services.active_order_rebuild_service import ActiveOrderRebuildService
from app.services.market_order_execution_service import MarketOrderExecutionService
from app.services.market_tick_matching_service import (
    MarketTickEventValidationError,
    MarketTickMatchingService,
    UnsupportedMarketTickEventError,
)
from app.services.order_arrival_matching_service import OrderArrivalMatchingService

__all__ = [
    "DerivativeMatchingStrategy",
    "AcceptedOrderEventService",
    "ActiveOrderRebuildService",
    "FuturesProductStrategy",
    "MatchingStrategy",
    "MatchingStrategyRegistry",
    "OptionProductStrategy",
    "OrderCancellationService",
    "MarketOrderExecutionService",
    "MarketTickMatchingService",
    "MarketTickEventValidationError",
    "UnsupportedMarketTickEventError",
    "UnsupportedOrderEventError",
    "OrderArrivalMatchingService",
    "OrderService",
    "ProductStrategy",
    "ProductStrategyRegistry",
    "get_order_cancellation_service",
    "get_order_service",
    "product_strategy_registry",
    "resolve_product_strategy",
]
