import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from app.enums.order_enums import OrderDirection
from app.infrastructure.order_event_publisher import OrderEventPublisher
from app.infrastructure.redis_keys import ORDER_EVENT_STREAM


def test_publish_writes_expected_redis_stream_fields():
    redis_client = Mock()
    redis_client.xadd.return_value = b"1-0"
    event = SimpleNamespace(
        event_id="EVT-1",
        event_type="ORDER_ACCEPTED",
        payload={
            "price": Decimal("3500.000000"),
            "trading_day": date(2026, 7, 20),
            "accepted_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
            "direction": OrderDirection.BUY,
        },
    )

    message_id = OrderEventPublisher(redis_client).publish(event)

    assert message_id == "1-0"
    stream = redis_client.xadd.call_args.args[0]
    fields = redis_client.xadd.call_args.kwargs["fields"]
    assert stream == ORDER_EVENT_STREAM
    assert fields["event_id"] == "EVT-1"
    assert fields["event_type"] == "ORDER_ACCEPTED"
    payload = json.loads(fields["payload"])
    assert payload["price"] == "3500.000000"
    assert payload["trading_day"] == "2026-07-20"
    assert payload["direction"] == "BUY"
