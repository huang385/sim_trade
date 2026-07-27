from decimal import Decimal

import pytest

from app.services.fee_calculator import (
    FeeBucketEntry,
    FeeBucketKey,
    FeeCalculator,
)


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


def test_same_bucket_total_is_independent_of_detail_count():
    """同一费用桶的总手续费不能因持仓明细拆分方式而改变。"""

    key = FeeBucketKey(
        resolved_offset_flag="CLOSE_TODAY",
        commission_type="BY_AMOUNT",
        commission_parameter=Decimal("0.000001000015"),
        commission_contract_multiplier=Decimal("10"),
    )
    one_detail = FeeCalculator.calculate_bucket_allocations(
        price=Decimal("3520"),
        entries=[FeeBucketEntry(key=key, volume=2)],
    )
    two_details = FeeCalculator.calculate_bucket_allocations(
        price=Decimal("3520"),
        entries=[
            FeeBucketEntry(key=key, volume=1),
            FeeBucketEntry(key=key, volume=1),
        ],
    )

    assert one_detail == [Decimal("0.070401")]
    assert two_details == [
        Decimal("0.035201"),
        Decimal("0.035200"),
    ]
    assert sum(one_detail) == sum(two_details) == Decimal("0.070401")


def test_different_offset_flags_are_calculated_as_separate_buckets():
    """普通CLOSE跨今昨仓时，平昨和平今应分别计算后再汇总。"""

    entries = [
        FeeBucketEntry(
            key=FeeBucketKey(
                resolved_offset_flag="CLOSE_YESTERDAY",
                commission_type="BY_VOLUME",
                commission_parameter=Decimal("3"),
                commission_contract_multiplier=Decimal("10"),
            ),
            volume=3,
        ),
        FeeBucketEntry(
            key=FeeBucketKey(
                resolved_offset_flag="CLOSE_TODAY",
                commission_type="BY_VOLUME",
                commission_parameter=Decimal("6"),
                commission_contract_multiplier=Decimal("10"),
            ),
            volume=2,
        ),
    ]

    result = FeeCalculator.calculate_bucket_allocations(
        price=Decimal("3520"),
        entries=entries,
    )

    assert result == [Decimal("9.000000"), Decimal("12.000000")]
    assert sum(result) == Decimal("21.000000")
