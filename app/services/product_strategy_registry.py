from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.common.exceptions import BusinessRuleError
from app.enums.instrument_enums import InstrumentType
from app.enums.product_enums import ProductFamily


@runtime_checkable
class ProductStrategy(Protocol):
    """订单与交易编排使用的最小产品身份接口。"""

    family: ProductFamily
    instrument_types: frozenset[str]

    @property
    def is_option(self) -> bool: ...


@dataclass(frozen=True)
class FuturesProductStrategy:
    family: ProductFamily = ProductFamily.FUTURES
    instrument_types: frozenset[str] = frozenset(
        {InstrumentType.FUTURES.value}
    )

    @property
    def is_option(self) -> bool:
        return False


@dataclass(frozen=True)
class OptionProductStrategy:
    family: ProductFamily = ProductFamily.OPTIONS
    instrument_types: frozenset[str] = frozenset(
        {
            InstrumentType.FUTURES_OPTION.value,
            InstrumentType.INDEX_OPTION.value,
        }
    )

    @property
    def is_option(self) -> bool:
        return True


@dataclass(frozen=True)
class StockProductStrategy:
    """股票只注册产品身份；撮合策略在下一阶段才会注册。"""

    family: ProductFamily = ProductFamily.STOCKS
    instrument_types: frozenset[str] = frozenset({InstrumentType.STOCK.value})

    @property
    def is_option(self) -> bool:
        return False


@dataclass(frozen=True)
class ConvertibleBondProductStrategy:
    """可转债是独立现金证券产品，不从证券代码推断产品身份。"""

    family: ProductFamily = ProductFamily.CONVERTIBLE_BONDS
    instrument_types: frozenset[str] = frozenset(
        {InstrumentType.CONVERTIBLE_BOND.value}
    )

    @property
    def is_option(self) -> bool:
        return False


class ProductStrategyRegistry:
    """只根据服务端产品事实分发；未注册产品禁止回退到期货。"""

    def __init__(self) -> None:
        self._strategies: dict[str, ProductStrategy] = {}

    @staticmethod
    def normalize_instrument_type(instrument_type: object) -> str:
        value = getattr(instrument_type, "value", instrument_type)
        return str(value or "").strip().upper()

    def register(self, strategy: ProductStrategy) -> None:
        if not isinstance(strategy, ProductStrategy):
            raise TypeError("产品策略未实现 ProductStrategy 接口")
        if not strategy.instrument_types:
            raise ValueError("产品策略必须声明至少一种合约类型")
        for instrument_type in strategy.instrument_types:
            normalized = self.normalize_instrument_type(instrument_type)
            if not normalized:
                raise ValueError("合约类型不能为空")
            if normalized in self._strategies:
                raise ValueError(f"合约类型重复注册: {normalized}")
            self._strategies[normalized] = strategy

    def resolve(self, instrument_type: object) -> ProductStrategy:
        normalized = self.normalize_instrument_type(instrument_type)
        strategy = self._strategies.get(normalized)
        if strategy is None:
            raise BusinessRuleError(
                f"合约产品类型尚未实现: {normalized or '<empty>'}",
                error_code="PRODUCT_STRATEGY_NOT_REGISTERED",
            )
        return strategy


product_strategy_registry = ProductStrategyRegistry()
product_strategy_registry.register(FuturesProductStrategy())
product_strategy_registry.register(OptionProductStrategy())
product_strategy_registry.register(StockProductStrategy())
product_strategy_registry.register(ConvertibleBondProductStrategy())


def resolve_product_strategy(instrument_type: object) -> ProductStrategy:
    return product_strategy_registry.resolve(instrument_type)
