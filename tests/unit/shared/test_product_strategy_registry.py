import pytest

from app.common.exceptions import BusinessRuleError
from app.enums.instrument_enums import InstrumentType
from app.services.product_strategy_registry import (
    FuturesProductStrategy,
    OptionProductStrategy,
    ProductFamily,
    ProductStrategyRegistry,
    resolve_product_strategy,
)


def test_futures_selects_futures_strategy():
    strategy = resolve_product_strategy(InstrumentType.FUTURES)

    assert isinstance(strategy, FuturesProductStrategy)
    assert strategy.family == ProductFamily.FUTURES
    assert strategy.is_option is False


@pytest.mark.parametrize(
    "instrument_type",
    [InstrumentType.FUTURES_OPTION, InstrumentType.INDEX_OPTION],
)
def test_options_select_option_strategy(instrument_type):
    strategy = resolve_product_strategy(instrument_type)

    assert isinstance(strategy, OptionProductStrategy)
    assert strategy.family == ProductFamily.OPTIONS
    assert strategy.is_option is True


@pytest.mark.parametrize("instrument_type", ["STOCK", "UNKNOWN", "", None])
def test_unregistered_product_fails_explicitly(instrument_type):
    with pytest.raises(BusinessRuleError) as exc_info:
        resolve_product_strategy(instrument_type)

    assert exc_info.value.error_code == "PRODUCT_STRATEGY_NOT_REGISTERED"


def test_unknown_product_never_falls_back_to_futures():
    registry = ProductStrategyRegistry()
    registry.register(FuturesProductStrategy())

    with pytest.raises(BusinessRuleError):
        registry.resolve("STOCK")


def test_duplicate_instrument_type_registration_is_rejected():
    registry = ProductStrategyRegistry()
    registry.register(FuturesProductStrategy())

    with pytest.raises(ValueError, match="重复注册"):
        registry.register(FuturesProductStrategy())
