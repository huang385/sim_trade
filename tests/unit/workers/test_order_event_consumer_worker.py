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
    arrival_matching_service=None,
    cash_security_arrival_matching_service=None,
    cash_security_event_service=None,
    market_order_execution_service=None,
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
        arrival_matching_service=arrival_matching_service,
        cash_security_arrival_matching_service=(
            cash_security_arrival_matching_service
        ),
        market_order_execution_service=market_order_execution_service,
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


def test_accepted_order_triggers_arrival_matching_before_ack():
    arrival_matching_service = Mock()
    arrival_matching_service.match_if_ready.return_value = (
        SimpleNamespace(action="SETTLED")
    )
    worker, stream_consumer, _, _ = make_worker(
        arrival_matching_service=arrival_matching_service
    )

    result = worker.handle_message("1-0", FIELDS)

    assert result == "acknowledged"
    arrival_matching_service.match_if_ready.assert_called_once_with(
        order_id="O-1",
        exchange_id="DCE",
        order_book_id="JD2609",
        symbol="JD2609",
        order_snapshot=None,
    )
    stream_consumer.acknowledge.assert_called_once_with("1-0")


def test_cash_security_accepted_order_triggers_its_own_arrival_matching():
    cash_arrival = Mock()
    cash_arrival.match_if_ready.return_value = SimpleNamespace(
        action="WAITING_FOR_LIVE_TICK"
    )
    cash_event_service = Mock()
    cash_event_service.is_cash_security_event.return_value = True
    cash_event_service.process.return_value = SimpleNamespace(
        event_id="EVT-CASH-1",
        event_type="STOCK_ORDER_ACCEPTED",
        order_id="SO-1",
        exchange_id="SSE",
        order_book_id="600519.XSHG",
        symbol="600519",
        action="REGISTERED",
    )
    worker, stream_consumer, event_service, _ = make_worker(
        cash_security_event_service=cash_event_service,
        cash_security_arrival_matching_service=cash_arrival,
    )

    result = worker.handle_message(
        "cash-1",
        {
            "event_id": "EVT-CASH-1",
            "event_type": "STOCK_ORDER_ACCEPTED",
            "payload": '{"order_id":"SO-1"}',
        },
    )

    assert result == "acknowledged"
    event_service.process.assert_not_called()
    cash_arrival.match_if_ready.assert_called_once_with(
        order_id="SO-1",
        exchange_id="SSE",
        order_book_id="600519.XSHG",
        symbol="600519",
    )
    stream_consumer.acknowledge.assert_called_once_with("cash-1")


def test_market_order_executes_and_cancels_before_ack():
    market_execution = Mock()
    worker, stream_consumer, event_service, _ = make_worker(
        market_order_execution_service=market_execution
    )
    snapshot = object()
    event_service.process.return_value = SimpleNamespace(
        event_id="EVT-1",
        event_type="ORDER_ACCEPTED",
        order_id="O-1",
        exchange_id="DCE",
        symbol="JD2609",
        action="MARKET_READY",
        order_snapshot=snapshot,
    )

    result = worker.handle_message("1-0", FIELDS)

    assert result == "acknowledged"
    market_execution.execute.assert_called_once_with(
        order_id="O-1", order_snapshot=snapshot
    )
    stream_consumer.acknowledge.assert_called_once_with("1-0")


def test_arrival_matching_failure_keeps_order_event_pending():
    arrival_matching_service = Mock()
    arrival_matching_service.match_if_ready.side_effect = ConnectionError(
        "redis down"
    )
    worker, stream_consumer, _, _ = make_worker(
        arrival_matching_service=arrival_matching_service
    )

    result = worker.handle_message("1-0", FIELDS)

    assert result == "retry"
    stream_consumer.acknowledge.assert_not_called()


def test_non_accepted_event_does_not_trigger_arrival_matching():
    arrival_matching_service = Mock()
    worker, _stream_consumer, event_service, _ = make_worker(
        arrival_matching_service=arrival_matching_service
    )
    event_service.process.return_value = SimpleNamespace(
        event_id="EVT-2",
        event_type="ORDER_PARTIALLY_FILLED",
        order_id="O-1",
        exchange_id="DCE",
        symbol="JD2609",
        action="UPDATED",
    )

    result = worker.handle_message("2-0", FIELDS)

    assert result == "acknowledged"
    arrival_matching_service.match_if_ready.assert_not_called()


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
