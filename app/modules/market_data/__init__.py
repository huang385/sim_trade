"""行情模块公共入口，惰性加载应用服务。"""

__all__ = [
    "LiveMarketSnapshotService", "MarketDataCodeMappingService",
    "MarketDataCodeMappingSnapshot", "MarketDataProcessAction",
    "MarketDataProvider", "MarketDataService", "MarketDataSubscription",
    "MarketSubscriptionService", "MarketTickNormalizationError",
    "MarketTickNormalizer", "MarketTickValidationError",
    "MarketTickValidationService",
    "OptionMarketPreSubscriptionService",
    "get_option_market_pre_subscription_service",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    from app.modules.market_data import facade

    return getattr(facade, name)
