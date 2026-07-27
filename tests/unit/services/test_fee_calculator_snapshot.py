from decimal import Decimal

import pytest

from app.services.fee_calculator import FeeCalculator


def test_by_volume_snapshot_is_independent_of_fill_price():
    low = FeeCalculator.calculate_from_snapshot(
        price=Decimal("3500"),
        volume=3,
        commission_type="BY_VOLUME",
        commission_parameter=Decimal("3"),
        contract_multiplier=Decimal("10"),
    )
    high = FeeCalculator.calculate_from_snapshot(
        price=Decimal("3522"),
        volume=3,
        commission_type="BY_VOLUME",
        commission_parameter=Decimal("3"),
        contract_multiplier=Decimal("10"),
    )

    assert low == high == Decimal("9.000000")


@pytest.mark.parametrize(
    ("fill_price", "expected"),
    [
        ("3522", Decimal("10.566000")),
        ("3480", Decimal("10.440000")),
    ],
)
def test_by_amount_snapshot_uses_actual_fill_price(fill_price, expected):
    result = FeeCalculator.calculate_from_snapshot(
        price=Decimal(fill_price),
        volume=3,
        commission_type="BY_AMOUNT",
        commission_parameter=Decimal("0.0001"),
        contract_multiplier=Decimal("10"),
    )

    assert result == expected
