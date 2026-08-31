from types import SimpleNamespace
from unittest.mock import Mock

from app.services.market_tick_matching_service import ParsedMarketTickEvent
from app.services.order_arrival_matching_service import (
    OrderArrivalMatchingService,
)


def test_waits_when_current_subscription_has_no_ready_tick():
    snapshot_service = Mock()
    snapshot_service.get_matching_event.return_value = None
    matching_service = Mock()
    service = OrderArrivalMatchingService(
        live_market_snapshot_service=snapshot_service,
        matching_service=matching_service,
    )

    result = service.match_if_ready(
        order_id="O-1",
        exchange_id="DCE",
        order_book_id="JD2609",
        symbol="JD2609",
    )

    assert result.action == "WAITING_FOR_LIVE_TICK"
    matching_service.process.assert_not_called()


def test_ready_tick_reuses_existing_matching_service():
    event = SimpleNamespace(
        stream_message_id="123-0",
        parsed_event=Mock(spec=ParsedMarketTickEvent),
    )
    snapshot_service = Mock()
    snapshot_service.get_matching_event.return_value = event
    matching_service = Mock()
    matching_service.process_candidate_order.return_value = SimpleNamespace(
        settled_count=1
    )
    service = OrderArrivalMatchingService(
        live_market_snapshot_service=snapshot_service,
        matching_service=matching_service,
    )

    result = service.match_if_ready(
        order_id="O-1",
        exchange_id="DCE",
        order_book_id="JD2609",
        symbol="JD2609",
    )

    assert result.action == "SETTLED"
    matching_service.process_candidate_order.assert_called_once_with(
        order_id="O-1",
        stream_message_id="123-0",
        event=event.parsed_event,
        order_snapshot=None,
    )


def test_order_arrival_reuses_its_own_bootstrap_snapshot():
    event = SimpleNamespace(
        stream_message_id="snapshot-123-0",
        parsed_event=Mock(spec=ParsedMarketTickEvent),
    )
    snapshot_service = Mock()
    snapshot_service.get_matching_event.return_value = event
    matching_service = Mock()
    matching_service.process_candidate_order.return_value = SimpleNamespace(
        settled_count=1
    )
    order_snapshot = SimpleNamespace(
        price_snapshot_source="YMM_DATA_SDK",
        price_snapshot_stream_message_id="snapshot-123-0",
    )
    service = OrderArrivalMatchingService(
        live_market_snapshot_service=snapshot_service,
        matching_service=matching_service,
    )

    result = service.match_if_ready(
        order_id="O-1",
        exchange_id="DCE",
        order_book_id="JD2611C3300",
        symbol="jd2611-C-3300",
        order_snapshot=order_snapshot,
    )

    assert result.action == "SETTLED"
    snapshot_service.get_matching_event.assert_called_once_with(
        exchange_id="DCE",
        order_book_id="JD2611C3300",
        symbol="jd2611-C-3300",
        allow_bootstrap_snapshot=True,
        expected_bootstrap_stream_message_id="snapshot-123-0",
    )
