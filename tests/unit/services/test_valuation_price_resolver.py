from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from app.services.trading_day_service import TradingSessionState
from app.services.valuation import (
    ValuationPriceResolver,
    ValuationPriceSource,
)


TRADING_DAY = date(2026, 9, 3)


def instrument():
    return SimpleNamespace(
        exchange_id="DCE",
        product_id="JD",
        instrument_type="FUTURES",
    )


def carried_detail():
    return SimpleNamespace(
        remaining_volume=1,
        open_trading_day=date(2026, 9, 2),
        pnl_base_price=Decimal("4074"),
    )


def market_tick(*, trading_day: str, price: str = "4080"):
    return {
        "source": "YMM_LIVE_DATA",
        "ingest_type": "LIVE_CALLBACK",
        "trading_day": trading_day,
        "last_price": price,
    }


def test_current_day_realtime_price_always_has_priority():
    trading_day_service = Mock()
    resolver = ValuationPriceResolver(trading_day_service)

    result = resolver.resolve_position(
        object(),
        instrument=instrument(),
        market_values=market_tick(trading_day="2026-09-03"),
        details=[carried_detail()],
        expected_trading_day=TRADING_DAY,
    )

    assert result.price == Decimal("4080")
    assert result.source == ValuationPriceSource.REALTIME
    trading_day_service.session_state.assert_not_called()


def test_closed_carried_position_uses_verified_settlement_baseline():
    trading_day_service = Mock()
    trading_day_service.session_state.return_value = TradingSessionState.CLOSED
    resolver = ValuationPriceResolver(trading_day_service)

    result = resolver.resolve_position(
        object(),
        instrument=instrument(),
        market_values=market_tick(trading_day="2026-09-02"),
        details=[carried_detail()],
        expected_trading_day=TRADING_DAY,
    )

    assert result.price == Decimal("4074")
    assert result.source == ValuationPriceSource.SETTLEMENT


def test_open_session_does_not_fall_back_to_settlement():
    trading_day_service = Mock()
    trading_day_service.session_state.return_value = TradingSessionState.OPEN
    resolver = ValuationPriceResolver(trading_day_service)

    result = resolver.resolve_position(
        object(),
        instrument=instrument(),
        market_values=market_tick(trading_day="2026-09-02"),
        details=[carried_detail()],
        expected_trading_day=TRADING_DAY,
    )

    assert result is None


def test_same_day_position_is_not_mistaken_for_settled_carry():
    trading_day_service = Mock()
    trading_day_service.session_state.return_value = TradingSessionState.CLOSED
    resolver = ValuationPriceResolver(trading_day_service)
    detail = carried_detail()
    detail.open_trading_day = TRADING_DAY

    result = resolver.resolve_position(
        object(),
        instrument=instrument(),
        market_values={},
        details=[detail],
        expected_trading_day=TRADING_DAY,
    )

    assert result is None
    trading_day_service.session_state.assert_not_called()


def test_inconsistent_detail_baselines_fail_closed():
    first = carried_detail()
    second = carried_detail()
    second.pnl_base_price = Decimal("4075")
    resolver = ValuationPriceResolver(Mock())

    assert resolver.settlement_price(
        [first, second], expected_trading_day=TRADING_DAY
    ) is None
