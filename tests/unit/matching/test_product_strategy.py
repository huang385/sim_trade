from unittest.mock import Mock

import pytest

from app.enums.product_enums import ProductFamily
from app.matching.product_strategy import (
    DerivativeMatchingStrategy,
    MatchingStrategyRegistry,
)


def test_derivative_matching_strategy_covers_futures_and_options():
    engine = Mock()
    strategy = DerivativeMatchingStrategy(engine)

    assert strategy.families == {
        ProductFamily.FUTURES,
        ProductFamily.OPTIONS,
    }


def test_matching_strategy_registry_rejects_unregistered_family():
    registry = MatchingStrategyRegistry()

    with pytest.raises(ValueError, match="尚未实现"):
        registry.resolve(ProductFamily.FUTURES)
