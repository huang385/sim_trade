"""旧撮合路径兼容层；实现已迁入 orders 模块。"""

from app.modules.orders.matching import (
    DerivativeMatchingStrategy,
    MatchingStrategy,
    MatchingStrategyRegistry,
)

__all__ = [
    "DerivativeMatchingStrategy",
    "MatchingStrategy",
    "MatchingStrategyRegistry",
]
