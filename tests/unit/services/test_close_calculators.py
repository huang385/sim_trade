from decimal import Decimal

import pytest

from app.services.margin_release_calculator import MarginReleaseCalculator
from app.services.realized_pnl_calculator import RealizedPnlCalculator


@pytest.mark.parametrize(
    ("direction", "open_price", "close_price", "expected"),
    [
        ("SELL", "3500", "3522", "660.000000"),
        ("SELL", "3500", "3480", "-600.000000"),
        ("BUY", "3500", "3480", "600.000000"),
        ("BUY", "3500", "3522", "-660.000000"),
    ],
)
def test_realized_pnl_uses_position_direction_formula(
    direction,
    open_price,
    close_price,
    expected,
):
    assert RealizedPnlCalculator.calculate(
        close_direction=direction,
        open_price=Decimal(open_price),
        close_price=Decimal(close_price),
        volume=3,
        contract_multiplier=Decimal("10"),
    ) == Decimal(expected)


def test_margin_release_is_proportional_and_last_close_consumes_tail():
    assert MarginReleaseCalculator.calculate(
        remaining_margin=Decimal("100.000001"),
        close_volume=1,
        remaining_volume_before_close=3,
    ) == Decimal("33.333334")
    assert MarginReleaseCalculator.calculate(
        remaining_margin=Decimal("66.666667"),
        close_volume=2,
        remaining_volume_before_close=2,
    ) == Decimal("66.666667")
