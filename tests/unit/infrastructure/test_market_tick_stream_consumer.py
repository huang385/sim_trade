from unittest.mock import Mock

import pytest
from redis.exceptions import ResponseError

from app.infrastructure.market_tick_stream_consumer import MarketTickStreamConsumer


def make_consumer(redis_client=None):
    return MarketTickStreamConsumer(
        redis_client or Mock(),
        stream_name="stream:market-ticks",
        group_name="group:matching-engine",
        consumer_name="matching-1",
        dead_letter_stream="stream:market-ticks:dead-letter",
        failure_ttl_seconds=60,
    )


def test_group_is_created_from_dollar_without_resetting_existing_group():
    redis_client = Mock()
    make_consumer(redis_client).ensure_group()
    redis_client.xgroup_create.assert_called_once_with(
        "stream:market-ticks",
        "group:matching-engine",
        id="$",
        mkstream=True,
    )
    redis_client.xgroup_create.side_effect = ResponseError("BUSYGROUP exists")
    make_consumer(redis_client).ensure_group()


def test_non_busy_group_error_is_not_hidden():
    redis_client = Mock()
    redis_client.xgroup_create.side_effect = ResponseError("redis down")
    with pytest.raises(ResponseError):
        make_consumer(redis_client).ensure_group()


def test_xautoclaim_and_redis5_fallback_recover_pending():
    redis_client = Mock()
    redis_client.xpending.return_value = {"pending": 1}
    redis_client.xautoclaim.return_value = [
        "0-0",
        [("1-0", {"event_id": "TICK-1"})],
        [],
    ]
    assert make_consumer(redis_client).claim_stale_messages(
        pending_idle_ms=60000, batch_size=10
    )[0][0] == "1-0"

    redis_client.xautoclaim.side_effect = ResponseError("unknown command XAUTOCLAIM")
    redis_client.xpending_range.return_value = [
        {"message_id": "2-0", "time_since_delivered": 60001}
    ]
    redis_client.xclaim.return_value = [("2-0", {"event_id": "TICK-2"})]
    assert make_consumer(redis_client).claim_stale_messages(
        pending_idle_ms=60000, batch_size=10
    )[0][0] == "2-0"


def test_failure_counter_has_ttl_and_dead_letter_has_context():
    redis_client = Mock()
    pipeline = redis_client.pipeline.return_value
    pipeline.execute.return_value = [3, True]
    consumer = make_consumer(redis_client)
    assert consumer.increment_failure("1-0") == 3
    pipeline.expire.assert_called_once_with("market_matching_failure:1-0", 60)
    redis_client.xadd.return_value = "9-0"
    consumer.publish_dead_letter(
        source_message_id="1-0",
        fields={
            "event_id": "TICK-1",
            "event_type": "UNKNOWN",
            "exchange_id": "SHFE",
            "symbol": "AG2609",
            "payload": "{}",
        },
        error="invalid",
    )
    fields = redis_client.xadd.call_args.kwargs["fields"]
    assert fields["source_message_id"] == "1-0"
    assert fields["consumer_name"] == "matching-1"
