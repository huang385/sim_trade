from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.services.cash_security_position_replay_service import (
    CashSecurityPositionReplayProjection,
)


DAY = date(2026, 8, 19)


def _position():
    # Deliberately wrong mutable values: replay must not use them as input.
    return SimpleNamespace(
        account_id="A1", exchange_id="SSE", symbol="600000", instrument_type="STOCK",
        total_volume=999, today_volume=999, yesterday_volume=0, pending_share_volume=0,
        frozen_volume=0, settlement_locked_volume=999, available_volume=0,
        position_cost=Decimal("1"), daily_pnl_base_cost=Decimal("1"),
        yesterday_pnl_base_cost=Decimal("1"), today_pnl_base_cost=Decimal("0"),
        daily_pnl_base_established=True, average_open_price=Decimal("1"),
    )


def _adjustment(kind: str, *, row_id: int, total: int = 0, yesterday: int = 0,
                pending: int = 0, available: int = 0, cost: str = "0",
                daily_base: str = "0", payload: dict | None = None):
    return SimpleNamespace(
        id=row_id, effective_trading_day=DAY, action_id="CA-1", action_version=2,
        component_id="COMP-1", business_version="2", adjustment_type=kind,
        total_volume_delta=total, today_volume_delta=0, yesterday_volume_delta=yesterday,
        pending_volume_delta=pending, available_volume_delta=available,
        frozen_volume_delta=0, settlement_locked_volume_delta=0,
        position_cost_delta=Decimal(cost), daily_pnl_base_cost_delta=Decimal(daily_base),
        average_open_price_after=None, replay_payload=payload or {},
    )


def test_replay_restores_listed_shares_when_mutable_position_was_tampered():
    projection = CashSecurityPositionReplayProjection.replay(
        position=_position(), trades=(), trading_day=DAY,
        adjustments=(
            _adjustment(
                "REPLAY_OPENING_BALANCE", row_id=1, total=100, yesterday=100,
                available=100, cost="1000", daily_base="1000",
                payload={
                    "yesterday_pnl_base_cost": "1000",
                    "today_pnl_base_cost": "0",
                    "daily_pnl_base_established": True,
                },
            ),
            _adjustment("SHARES_LISTED", row_id=2, total=10, yesterday=10, available=10),
        ),
    )

    assert projection.authoritative is True
    assert projection.total_volume == projection.yesterday_volume == projection.available_volume == 110
    assert projection.position_cost == Decimal("1000.000000")
    assert projection.average_open_price == Decimal("9.090909")


def test_replay_applies_bond_maturity_instead_of_validating_current_position():
    position = _position()
    position.instrument_type = "CONVERTIBLE_BOND"
    projection = CashSecurityPositionReplayProjection.replay(
        position=position, trades=(), trading_day=DAY,
        adjustments=(
            _adjustment(
                "REPLAY_OPENING_BALANCE", row_id=1, total=100, yesterday=100,
                available=100, cost="1000", daily_base="1000",
                payload={"yesterday_pnl_base_cost": "1000", "daily_pnl_base_established": True},
            ),
            _adjustment(
                "BOND_MATURITY_RETIRED", row_id=2, total=-100, yesterday=-100,
                available=-100, cost="-1000", daily_base="-1000",
            ),
        ),
    )

    assert projection.total_volume == projection.yesterday_volume == 0
    assert projection.position_cost == projection.daily_pnl_base_cost == Decimal("0.000000")
