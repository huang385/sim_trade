from types import SimpleNamespace
from unittest.mock import Mock

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
        exchange_id="DCE",
        symbol="JD2609",
    )

    assert result.action == "WAITING_FOR_LIVE_TICK"
    matching_service.process.assert_not_called()


def test_ready_tick_reuses_existing_matching_service():
    event = SimpleNamespace(
        stream_message_id="123-0",
        fields={"event_id": "TICK-1"},
    )
    snapshot_service = Mock()
    snapshot_service.get_matching_event.return_value = event
    matching_service = Mock()
    matching_service.process.return_value = SimpleNamespace(
        settled_count=1
    )
    service = OrderArrivalMatchingService(
        live_market_snapshot_service=snapshot_service,
        matching_service=matching_service,
    )

    result = service.match_if_ready(
        exchange_id="DCE",
        symbol="JD2609",
    )

    assert result.action == "SETTLED"
    matching_service.process.assert_called_once_with(
        stream_message_id="123-0",
        fields=event.fields,
    )
