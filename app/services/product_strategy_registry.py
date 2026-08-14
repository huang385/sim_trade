"""旧 Service 路径兼容层；实现已迁入 orders 模块。"""

from app.modules.orders.product_registry import (
    FuturesProductStrategy,
    OptionProductStrategy,
    ProductStrategy,
    ProductStrategyRegistry,
    product_strategy_registry,
    resolve_product_strategy,
)
from app.shared.enums import ProductFamily

__all__ = [
    "FuturesProductStrategy",
    "OptionProductStrategy",
    "ProductFamily",
    "ProductStrategy",
    "ProductStrategyRegistry",
    "product_strategy_registry",
    "resolve_product_strategy",
]
