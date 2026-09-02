from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from sqlalchemy.exc import OperationalError

from app.services.accepted_order_event_service import (
    UnsupportedOrderEventError,
)
from app.workers.order_event_consumer_worker import OrderEventConsumerWorker


def make_worker(
    *,
    process_side_effect=None,
    failure_count=1,
    cash_security_event_service=None,
):
    db = Mock()
    session_context = MagicMock()
    session_context.__enter__.return_value = db
    session_factory = Mock(return_value=session_context)
    stream_consumer = Mock()
    stream_consumer.consumer_name = "consumer-1"
    stream_consumer.increment_failure.return_value = failure_count
    stream_consumer.claim_stale_messages.return_value = []
    stream_consumer.read_new_messages.return_value = []
    event_service = Mock()
    if process_side_effect is None:
        event_service.process.return_value = SimpleNamespace(
            event_id="EVT-1",
            event_type="ORDER_ACCEPTED",
            order_id="O-1",
            exchange_id="DCE",
            order_book_id="JD2609",
            symbol="JD2609",
            action="REGISTERED",
        )
    else:
        event_service.process.side_effect = process_side_effect
    worker = OrderEventConsumerWorker(
        session_factory=session_factory,
        stream_consumer=stream_consumer,
        event_service=event_service,
        cash_security_event_service=cash_security_event_service,
        batch_size=100,
        block_ms=1,
        pending_idle_ms=60000,
        max_retries=10,
        retry_interval_seconds=0,
    )
    return worker, stream_consumer, event_service, db


FIELDS = {
    "event_id": "EVT-1",
    "event_type": "ORDER_ACCEPTED",
    "payload": '{"order_id":"O-1"}',
}


def test_successful_message_is_acknowledged():
    worker, stream_consumer, _, _ = make_worker()

    result = worker.handle_message("1-0", FIELDS)

    assert result == "acknowledged"
    stream_consumer.acknowledge.assert_called_once_with("1-0")
    stream_consumer.clear_failure.assert_called_once_with("1-0")


def test_failed_message_is_not_acknowledged():
    worker, stream_consumer, _, db = make_worker(
        process_side_effect=ValueError("bad payload")
    )

    result = worker.handle_message("1-0", FIELDS)

    assert result == "retry"
    db.rollback.assert_called_once()
    stream_consumer.acknowledge.assert_not_called()


def test_database_failure_is_not_acknowledged():
    worker, stream_consumer, _, _ = make_worker(
        process_side_effect=OperationalError("select", {}, Exception("down"))
    )

    result = worker.handle_message("1-0", FIELDS)

    assert result == "retry"
    stream_consumer.acknowledge.assert_not_called()


def test_unknown_event_goes_directly_to_dead_letter_then_ack():
    worker, stream_consumer, _, _ = make_worker(
        process_side_effect=UnsupportedOrderEventError("unknown")
    )

    result = worker.handle_message("1-0", FIELDS)

    assert result == "dead_lettered"
    stream_consumer.publish_dead_letter.assert_called_once()
    stream_consumer.acknowledge.assert_called_once_with("1-0")


def test_max_failures_go_to_dead_letter_then_ack():
    worker, stream_consumer, _, _ = make_worker(
        process_side_effect=ValueError("bad"),
        failure_count=10,
    )

    result = worker.handle_message("1-0", FIELDS)

    assert result == "dead_lettered"
    stream_consumer.publish_dead_letter.assert_called_once()
    stream_consumer.acknowledge.assert_called_once_with("1-0")


def test_dead_letter_failure_keeps_original_unacknowledged():
    worker, stream_consumer, _, _ = make_worker(
        process_side_effect=ValueError("bad"),
        failure_count=10,
    )
    stream_consumer.publish_dead_letter.side_effect = ConnectionError(
        "redis down"
    )

    result = worker.handle_message("1-0", FIELDS)

    assert result == "retry"
    stream_consumer.acknowledge.assert_not_called()


def test_run_once_processes_recovered_pending_and_new_messages():
    worker, stream_consumer, _, _ = make_worker()
    stream_consumer.claim_stale_messages.return_value = [("1-0", FIELDS)]
    stream_consumer.read_new_messages.return_value = [("2-0", FIELDS)]

    result = worker.run_once()

    assert result.received == 2
    assert result.acknowledged == 2
    assert stream_consumer.acknowledge.call_count == 2


def test_deleted_pending_tombstone_is_acknowledged_without_business_processing():
    """Redis 5返回(message_id, None)时应清理PEL而不是让Worker崩溃。"""

    worker, stream_consumer, event_service, _ = make_worker()

    result = worker.handle_message("1-0", None)

    assert result == "acknowledged"
    event_service.process.assert_not_called()
    stream_consumer.acknowledge.assert_called_once_with("1-0")
    stream_consumer.clear_failure.assert_called_once_with("1-0")
