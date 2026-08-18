from unittest.mock import Mock

from app.workers.realtime_event_projection_worker import (
    RealtimeEventProjectionWorker,
)


def _worker(*, max_retries=3):
    consumer = Mock()
    event_store = Mock()
    return (
        RealtimeEventProjectionWorker(
            consumer=consumer,
            event_store=event_store,
            max_retries=max_retries,
        ),
        consumer,
        event_store,
    )


def test_stock_order_event_is_projected_and_acknowledged():
    worker, consumer, event_store = _worker()
    event_store.publish_projected_once.return_value = "2-0"

    worker._handle(
        "1-0",
        {
            "event_type": "STOCK_ORDER_ACCEPTED",
            "event_id": "SE-1",
            "aggregate_type": "ORDER",
            "aggregate_id": "SO-1",
            "business_version": "1",
            "payload": (
                '{"event_id":"SE-1","account_id":"A","order_id":"SO-1",'
                '"account_type":"SECURITIES_CASH","instrument_type":"STOCK"}'
            ),
        },
    )

    consumer.clear_failure.assert_called_once_with("1-0")
    consumer.acknowledge.assert_called_once_with("1-0")
    consumer.increment_failure.assert_not_called()
    consumer.publish_dead_letter.assert_not_called()
    event_store.publish_projected_once.assert_called_once()


def test_unknown_event_keeps_original_dead_letter_policy():
    worker, consumer, event_store = _worker(max_retries=1)
    consumer.increment_failure.return_value = 1

    worker._handle(
        "1-0",
        {
            "event_type": "UNRECOGNIZED_EVENT",
            "payload": "{}",
        },
    )

    consumer.increment_failure.assert_called_once_with("1-0")
    consumer.publish_dead_letter.assert_called_once()
    consumer.clear_failure.assert_called_once_with("1-0")
    consumer.acknowledge.assert_called_once_with("1-0")
    event_store.publish_projected_once.assert_not_called()
