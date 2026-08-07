from decimal import Decimal

import pytest

from app.services.pnl_calculator import (
    PnlCalculator,
    PnlDetailSnapshot,
    PositionPnlSnapshot,
)


def make_snapshot(
    *,
    direction: str = "LONG",
    details: tuple[PnlDetailSnapshot, ...] | None = None,
) -> PositionPnlSnapshot:
    return PositionPnlSnapshot(
        position_id="P001",
        account_id="A001",
        order_book_id="RB2610",
        exchange_id="SHFE",
        symbol="RB2610",
        direction=direction,
        contract_multiplier=Decimal("10"),
        persisted_unrealized_pnl=Decimal("0"),
        persisted_daily_position_pnl=Decimal("0"),
        details=details
        or (
            PnlDetailSnapshot(
                position_detail_id="PD001",
                open_price=Decimal("3400"),
                pnl_base_price=Decimal("3500"),
                remaining_volume=2,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("direction", "mark_price", "cumulative", "daily"),
    [
        ("LONG", "3520", "2400.000000", "400.000000"),
        ("LONG", "3480", "1600.000000", "-400.000000"),
        ("SHORT", "3520", "-2400.000000", "-400.000000"),
        ("SHORT", "3480", "-1600.000000", "400.000000"),
    ],
)
def test_position_pnl_separates_open_and_daily_base(
    direction,
    mark_price,
    cumulative,
    daily,
):
    result = PnlCalculator.calculate_position(
        mark_price=Decimal(mark_price),
        snapshot=make_snapshot(direction=direction),
    )

    assert result.cumulative_unrealized_pnl == Decimal(cumulative)
    assert result.daily_position_pnl == Decimal(daily)
    assert result.cash_unrealized_pnl == Decimal(cumulative)


def test_post_settlement_cash_valuation_uses_daily_basis_without_changing_audit_pnl():
    snapshot = make_snapshot()
    snapshot = PositionPnlSnapshot(
        **{
            **snapshot.__dict__,
            "uses_settlement_basis": True,
        }
    )

    result = PnlCalculator.calculate_position(
        mark_price=Decimal("3520"),
        snapshot=snapshot,
    )

    assert result.cumulative_unrealized_pnl == Decimal("2400.000000")
    assert result.daily_position_pnl == Decimal("400.000000")
    assert result.cash_unrealized_pnl == Decimal("400.000000")


def test_multiple_details_only_sum_remaining_volume():
    result = PnlCalculator.calculate_position(
        mark_price=Decimal("3520"),
        snapshot=make_snapshot(
            details=(
                PnlDetailSnapshot(
                    "PD-YESTERDAY",
                    Decimal("3400"),
                    Decimal("3500"),
                    1,
                ),
                PnlDetailSnapshot(
                    "PD-TODAY",
                    Decimal("3510"),
                    Decimal("3510"),
                    1,
                ),
                PnlDetailSnapshot(
                    "PD-CLOSED",
                    Decimal("3300"),
                    Decimal("3500"),
                    0,
                ),
            )
        ),
    )

    assert result.cumulative_unrealized_pnl == Decimal("1300.000000")
    assert result.daily_position_pnl == Decimal("300.000000")


def test_close_pnl_uses_two_independent_bases():
    result = PnlCalculator.calculate_close(
        position_direction="LONG",
        close_price=Decimal("3515"),
        open_price=Decimal("3400"),
        pnl_base_price=Decimal("3500"),
        volume=1,
        contract_multiplier=Decimal("10"),
    )

    assert result.realized_pnl == Decimal("1150.000000")
    assert result.daily_close_pnl == Decimal("150.000000")
