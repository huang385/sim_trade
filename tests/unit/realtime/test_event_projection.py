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


def test_order_margin_source_projects_to_absolute_order_update():
    event = RealtimeEventProjectionService.project(
        source_message_id="11-0",
        fields={
            "event_id": "E-MARGIN",
            "event_type": "ORDER_MARGIN_UPDATED",
            "aggregate_type": "ORDER",
            "aggregate_id": "O001",
            "business_version": "9",
            "payload": json.dumps(
                {
                    "event_id": "E-MARGIN",
                    "account_id": "A001",
                    "order_id": "O001",
                    "frozen_margin": "12000.000000",
                    "margin_risk_state": "NORMAL",
                    "updated_at": "2026-08-03T12:00:01+00:00",
                }
            ),
        },
    )

    assert event.event_type == RealtimeEventType.ORDER_UPDATED
    assert event.payload["frozen_margin"] == "12000.000000"
    assert event.payload["business_version"] == "9"


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
def test_risk_projection_uses_account_risk_version_not_outbox_id():
    payload = {
        "event_id": "RISK-1",
        "event_type": "RISK_STATE_CHANGED",
        "account_id": "A001",
        "risk_state": "MARGIN_DEFICIT",
        "risk_ratio": "1.10000000",
        "risk_available_cash": "-10.000000",
        "risk_version": 7,
        "trigger_reason": "RISK_LIMIT_EXCEEDED",
        "occurred_at": "2026-08-05T10:00:00+08:00",
    }
    event = RealtimeEventProjectionService.project(
        source_message_id="100-0",
        fields={
            "event_id": "RISK-1",
            "event_type": "RISK_STATE_CHANGED",
            "aggregate_type": "RISK",
            "aggregate_id": "A001",
            "business_version": "999",
            "payload": json.dumps(payload),
        },
    )
    assert event.business_version == "7"
    assert event.account_id == "A001"
    assert event.payload["risk_available_cash"] == "-10.000000"
