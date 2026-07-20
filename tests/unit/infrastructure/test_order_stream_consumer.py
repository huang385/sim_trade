from unittest.mock import Mock

import pytest
from redis.exceptions import ResponseError

from app.infrastructure.order_stream_consumer import OrderStreamConsumer


def make_consumer(redis_client=None):
    return OrderStreamConsumer(
        redis_client or Mock(),
        stream_name="stream:orders",
        group_name="group:test",
        consumer_name="consumer-1",
        dead_letter_stream="stream:orders:dead-letter",
        failure_ttl_seconds=60,
    )


def test_create_group_uses_zero_id_and_mkstream():
    redis_client = Mock()
    consumer = make_consumer(redis_client)

    consumer.ensure_group()

    redis_client.xgroup_create.assert_called_once_with(
        "stream:orders", "group:test", id="0-0", mkstream=True
    )


def test_existing_group_busy_error_is_ignored():
    redis_client = Mock()
    redis_client.xgroup_create.side_effect = ResponseError("BUSYGROUP exists")

    make_consumer(redis_client).ensure_group()


def test_non_busy_group_error_is_raised():
    redis_client = Mock()
    redis_client.xgroup_create.side_effect = ResponseError("connection error")

    with pytest.raises(ResponseError):
        make_consumer(redis_client).ensure_group()


def test_read_new_messages_flattens_redis_result():
    redis_client = Mock()
    redis_client.xreadgroup.return_value = [
        ("stream:orders", [("1-0", {"event_id": "EVT-1"})])
    ]

    messages = make_consumer(redis_client).read_new_messages(
        batch_size=10, block_ms=100
    )

    assert messages == [("1-0", {"event_id": "EVT-1"})]


def test_xautoclaim_recovers_stale_pending_message():
    redis_client = Mock()
    redis_client.xpending.return_value = {"pending": 1}
    redis_client.xautoclaim.return_value = [
        "0-0",
        [("1-0", {"event_id": "EVT-1"})],
        [],
    ]

    messages = make_consumer(redis_client).claim_stale_messages(
        pending_idle_ms=60000,
        batch_size=10,
    )

    assert messages[0][0] == "1-0"
    redis_client.xclaim.assert_not_called()


def test_redis5_falls_back_to_xpending_and_xclaim():
    redis_client = Mock()
    redis_client.xpending.return_value = {"pending": 1}
    redis_client.xautoclaim.side_effect = ResponseError(
        "unknown command `XAUTOCLAIM`"
    )
    redis_client.xpending_range.return_value = [
        {"message_id": "1-0", "time_since_delivered": 60001}
    ]
    redis_client.xclaim.return_value = [("1-0", {"event_id": "EVT-1"})]

    messages = make_consumer(redis_client).claim_stale_messages(
        pending_idle_ms=60000,
        batch_size=10,
    )

    assert messages[0][0] == "1-0"
    redis_client.xclaim.assert_called_once()


def test_redis5_deleted_pending_keeps_id_from_xpending():
    """XCLAIM返回(None, None)时必须保留XPENDING中的原始消息ID。"""

    redis_client = Mock()
    redis_client.xpending.return_value = {"pending": 1}
    redis_client.xautoclaim.side_effect = ResponseError(
        "unknown command `XAUTOCLAIM`"
    )
    redis_client.xpending_range.return_value = [
        {"message_id": "9-0", "time_since_delivered": 60001}
    ]
    redis_client.xclaim.return_value = [(None, None)]

    messages = make_consumer(redis_client).claim_stale_messages(
        pending_idle_ms=60000,
        batch_size=10,
    )

    assert messages == [("9-0", None)]


def test_failure_counter_sets_ttl():
    redis_client = Mock()
    pipeline = redis_client.pipeline.return_value
    pipeline.incr.return_value = pipeline
    pipeline.expire.return_value = pipeline
    pipeline.execute.return_value = [3, True]

    count = make_consumer(redis_client).increment_failure("1-0")

    assert count == 3
    pipeline.expire.assert_called_once_with(
        "order_event_failure:1-0", 60
    )


def test_dead_letter_contains_source_and_consumer():
    redis_client = Mock()
    redis_client.xadd.return_value = "2-0"
    consumer = make_consumer(redis_client)

    result = consumer.publish_dead_letter(
        source_message_id="1-0",
        fields={
            "event_id": "EVT-1",
            "event_type": "UNKNOWN",
            "payload": "{}",
        },
        error="unsupported",
    )

    assert result == "2-0"
    fields = redis_client.xadd.call_args.kwargs["fields"]
    assert fields["source_message_id"] == "1-0"
    assert fields["consumer_name"] == "consumer-1"
