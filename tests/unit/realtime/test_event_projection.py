import json
from unittest.mock import Mock

from app.realtime.event_enums import RealtimeEventType
from app.realtime.event_projection_service import (
    RealtimeEventProjectionService,
)
from app.realtime.event_store import RealtimeEventStore


def test_order_accepted_projection_adds_absolute_status():
    event = RealtimeEventProjectionService.project(
        source_message_id="10-0",
        fields={
            "event_id": "E001",
            "event_type": "ORDER_ACCEPTED",
            "aggregate_type": "ORDER",
            "aggregate_id": "O001",
            "business_version": "1",
            "payload": json.dumps(
                {
                    "event_id": "E001",
                    "account_id": "A001",
                    "order_id": "O001",
                    "accepted_at": "2026-08-03T12:00:00+00:00",
                    "frozen_margin": "8400.000000",
                }
            ),
        },
    )

    assert event.event_type == RealtimeEventType.ORDER_CREATED
    assert event.payload["status"] == "ACCEPTED"
    assert event.payload["frozen_margin"] == "8400.000000"
    assert event.version == "10-0"
    assert event.business_version == "1"
    assert event.payload["business_version"] == "1"


def test_projection_publish_is_atomic_and_idempotent():
    redis_client = Mock()
    redis_client.eval.side_effect = ["20-0", ""]
    store = RealtimeEventStore(redis_client)
    event = RealtimeEventProjectionService.project(
        source_message_id="10-0",
        fields={
            "event_id": "E001",
            "event_type": "ORDER_FILLED",
            "aggregate_type": "ORDER",
            "aggregate_id": "O001",
            "business_version": "2",
            "payload": json.dumps(
                {
                    "event_id": "E001",
                    "account_id": "A001",
                    "order_id": "O001",
                    "updated_at": "2026-08-03T12:00:01+00:00",
                    "status": "FILLED",
                }
            ),
        },
    )

    assert store.publish_projected_once(event) == "20-0"
    assert store.publish_projected_once(event) is None
    script = redis_client.eval.call_args_list[0].args[0]
    assert "EXISTS" in script
    assert "XADD" in script
    assert "SET" in script
    assert "is_greater" in script
    assert redis_client.eval.call_args_list[0].args[-1] == "2"


def test_stale_business_version_is_not_published_again():
    redis_client = Mock()
    redis_client.eval.return_value = "STALE"
    store = RealtimeEventStore(redis_client)
    event = RealtimeEventProjectionService.project(
        source_message_id="99-0",
        fields={
            "event_id": "E-LATE",
            "event_type": "ORDER_ACCEPTED",
            "aggregate_type": "ORDER",
            "aggregate_id": "O001",
            "business_version": "1",
            "payload": json.dumps(
                {
                    "event_id": "E-LATE",
                    "account_id": "A001",
                    "order_id": "O001",
                }
            ),
        },
    )

    assert store.publish_projected_once(event) is None
