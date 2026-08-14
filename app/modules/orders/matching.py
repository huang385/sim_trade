from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.matching.base import MatchingEngine
from app.matching.models import MatchResult, MatchingMarketData, MatchingOrder
from app.shared.enums import ProductFamily


@runtime_checkable
class MatchingStrategy(Protocol):
    """单一产品族撮合策略的最小公共端口。"""

    families: frozenset[ProductFamily]

    def match(
        self,
        order: MatchingOrder,
        market: MatchingMarketData,
    ) -> MatchResult: ...


@dataclass(frozen=True)
class DerivativeMatchingStrategy:
    """保持现有期货和期权成交语义的衍生品适配策略。"""

    engine: MatchingEngine
    families: frozenset[ProductFamily] = frozenset(
        {ProductFamily.FUTURES, ProductFamily.OPTIONS}
    )

    def match(
        self,
        order: MatchingOrder,
        market: MatchingMarketData,
    ) -> MatchResult:
        return self.engine.match(order, market)


class MatchingStrategyRegistry:
    """按产品族选择撮合策略；未注册产品族明确失败。"""

    def __init__(self) -> None:
        self._strategies: dict[ProductFamily, MatchingStrategy] = {}

    def register(self, strategy: MatchingStrategy) -> None:
        if not isinstance(strategy, MatchingStrategy):
            raise TypeError("撮合策略未实现 MatchingStrategy 接口")
        for family in strategy.families:
            if family in self._strategies:
                raise ValueError(f"产品撮合策略重复注册: {family.value}")
            self._strategies[family] = strategy

    def resolve(self, family: ProductFamily) -> MatchingStrategy:
        strategy = self._strategies.get(family)
        if strategy is None:
            raise ValueError(f"产品撮合策略尚未实现: {family.value}")
        return strategy
