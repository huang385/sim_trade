"""订单模块公共入口，使用惰性导出避免装配期循环依赖。"""

__all__ = [
    "AcceptedOrderEventService", "ActiveOrderRebuildService",
    "DerivativeMatchingStrategy", "FuturesProductStrategy",
    "MatchingStrategy", "MatchingStrategyRegistry", "OptionProductStrategy",
    "MarketOrderExecutionService", "MarketTickEventValidationError",
    "MarketTickMatchingService", "UnsupportedMarketTickEventError",
    "UnsupportedOrderEventError",
    "OrderArrivalMatchingService", "OrderCancellationService", "OrderService", "ProductStrategy",
    "ProductStrategyRegistry", "get_order_cancellation_service",
    "get_order_service", "product_strategy_registry", "resolve_product_strategy",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    from app.modules.orders import facade

    return getattr(facade, name)
