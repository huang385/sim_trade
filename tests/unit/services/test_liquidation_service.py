from decimal import Decimal
from types import SimpleNamespace

from app.services.liquidation_service import LiquidationService


def row(*, position_id, kind="FUTURES", direction="LONG", margin="1000", volume=10, available=10, underlying=None):
    position = SimpleNamespace(
        position_id=position_id,
        instrument_type=kind,
        direction=direction,
        used_margin=Decimal(margin),
        realtime_required_margin=Decimal(margin),
        total_volume=volume,
        available_volume=available,
        exchange_id="DCE",
        symbol=position_id,
    )
    instrument = SimpleNamespace(underlying_instrument_id=underlying)
    return position, instrument


def test_selects_highest_margin_release_and_minimum_volume():
    candidate = LiquidationService._select(
        [
            row(position_id="P1", margin="1000"),
            row(position_id="P2", margin="3000"),
        ],
        Decimal("450"),
    )
    assert candidate.position_id == "P2"
    assert candidate.volume == 2


def test_does_not_blindly_close_long_option_or_hedged_short_option():
    rows = [
        row(position_id="LONG", kind="FUTURES_OPTION", direction="LONG", underlying=7),
        row(position_id="SHORT", kind="FUTURES_OPTION", direction="SHORT", underlying=7),
        row(position_id="FUT", kind="FUTURES", direction="LONG", underlying=None),
    ]
    candidate = LiquidationService._select(rows, Decimal("1"))
    assert candidate.position_id == "FUT"


def test_no_candidate_when_only_protected_option_portfolio_exists():
    rows = [
        row(position_id="LONG", kind="INDEX_OPTION", direction="LONG", underlying=8),
        row(position_id="SHORT", kind="INDEX_OPTION", direction="SHORT", underlying=8),
    ]
    assert LiquidationService._select(rows, Decimal("1")) is None


def test_stock_never_enters_derivative_liquidation():
    assert (
        LiquidationService._select(
            [row(position_id="STOCK", kind="STOCK")],
            Decimal("1"),
        )
        is None
    )
