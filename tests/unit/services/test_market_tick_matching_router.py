from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from app.schemas.matching_schema import MarketTickMatchResult
from app.services.market_tick_matching_router import MarketTickMatchingRouter


def _result(*, candidates=0):
    return MarketTickMatchResult(
        candidate_count=candidates,
        matched_count=0,
        settled_count=0,
        idempotent_count=0,
        skipped_count=candidates,
    )


def _router(*, orders):
    event = SimpleNamespace(exchange_id="SSE", symbol="600519")
    session = MagicMock()
    session.__enter__.return_value = Mock()
    derivative = Mock()
    derivative.parse_event.return_value = event
    derivative.session_factory.return_value = session
    derivative.active_order_index.list_instrument_order_ids.return_value = {
        item.order_id for item in orders
    }
    derivative.order_repository.list_by_order_ids.return_value = orders
    derivative.process_routed_orders.return_value = _result(
        candidates=sum(
            item.instrument_type not in {"STOCK", "CONVERTIBLE_BOND"}
            for item in orders
        )
    )
    cash = Mock()
    cash.process_routed_orders.return_value = _result(
        candidates=sum(
            item.instrument_type in {"STOCK", "CONVERTIBLE_BOND"}
            for item in orders
        )
    )
    return MarketTickMatchingRouter(
        derivative_service=derivative,
        cash_security_service=cash,
    ), derivative, cash, event


@pytest.mark.parametrize("instrument_type", ["STOCK", "CONVERTIBLE_BOND"])
def test_cash_tick_reads_candidates_once_and_never_calls_derivative_executor(
    instrument_type,
):
    order = SimpleNamespace(order_id="C-1", instrument_type=instrument_type)
    router, derivative, cash, event = _router(orders=[order])

    result = router.process(stream_message_id="1-0", fields={"event_id": "T-1"})

    assert result.candidate_count == 1
    derivative.active_order_index.list_instrument_order_ids.assert_called_once_with(
        "SSE", "600519"
    )
    derivative.order_repository.list_by_order_ids.assert_called_once()
    assert derivative.order_repository.list_by_order_ids.call_args.args[1] == [
        "C-1"
    ]
    derivative.process_routed_orders.assert_not_called()
    cash.process_routed_orders.assert_called_once()
    assert cash.process_routed_orders.call_args.kwargs["order_ids"] == ["C-1"]
    assert cash.process_routed_orders.call_args.kwargs["event"] is event


def test_derivative_tick_reads_candidates_once_and_never_calls_cash_executor():
    order = SimpleNamespace(order_id="F-1", instrument_type="FUTURES")
    router, derivative, cash, _event = _router(orders=[order])

    result = router.process(stream_message_id="1-0", fields={"event_id": "T-1"})

    assert result.candidate_count == 1
    derivative.active_order_index.list_instrument_order_ids.assert_called_once()
    derivative.order_repository.list_by_order_ids.assert_called_once()
    derivative.process_routed_orders.assert_called_once()
    assert derivative.process_routed_orders.call_args.kwargs["order_ids"] == ["F-1"]
    cash.process_routed_orders.assert_not_called()


def test_mixed_candidates_are_loaded_once_and_routed_once_each():
    orders = [
        SimpleNamespace(order_id="F-1", instrument_type="FUTURES"),
        SimpleNamespace(order_id="S-1", instrument_type="STOCK"),
        SimpleNamespace(order_id="B-1", instrument_type="CONVERTIBLE_BOND"),
    ]
    router, derivative, cash, _event = _router(orders=orders)

    result = router.process(stream_message_id="1-0", fields={"event_id": "T-1"})

    assert result.candidate_count == 3
    derivative.active_order_index.list_instrument_order_ids.assert_called_once()
    derivative.order_repository.list_by_order_ids.assert_called_once()
    assert derivative.process_routed_orders.call_args.kwargs["order_ids"] == ["F-1"]
    assert cash.process_routed_orders.call_args.kwargs["order_ids"] == ["B-1", "S-1"]
