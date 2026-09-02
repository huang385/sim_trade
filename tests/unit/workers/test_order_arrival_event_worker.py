from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from app.enums.market_feed_enums import MarketFeedDomain
from app.workers.order_arrival_event_worker import OrderArrivalEventWorker


FIELDS = {
    "event_type": "ORDER_ACCEPTED",
    "payload": '{"order_id":"O-1"}',
}


def make_order(**overrides):
    values = {
        "order_id": "O-1",
        "instrument_type": "FUTURES",
        "order_type": "LIMIT",
        "status": "ACCEPTED",
        "remaining_volume": 1,
        "exchange_id": "DCE",
        "order_book_id": "JD2609",
        "symbol": "JD2609",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_worker(*, domain=MarketFeedDomain.FUTURES_MARKET, order=None):
    consumer = Mock()
    consumer.increment_failure.return_value = 1
    repository = Mock()
    repository.get_by_order_id.return_value = order or make_order()
    arrival = Mock()
    arrival.match_if_ready.return_value = SimpleNamespace(action="SETTLED")
    market_execution = Mock()
    worker = OrderArrivalEventWorker(
        domain=domain,
        session_factory=lambda: nullcontext(Mock()),
        stream_consumer=consumer,
        order_repository=repository,
        arrival_matching_service=arrival,
        market_order_execution_service=market_execution,
        batch_size=100,
        block_ms=1,
        pending_idle_ms=60_000,
        max_retries=3,
        retry_interval_seconds=0,
    )
    return worker, consumer, arrival, market_execution


def test_futures_arrival_is_matched_and_acknowledged():
    worker, consumer, arrival, _ = make_worker()

    assert worker.handle_message("1-0", FIELDS) == "acknowledged"
    arrival.match_if_ready.assert_called_once_with(
        order_id="O-1",
        exchange_id="DCE",
        order_book_id="JD2609",
        symbol="JD2609",
    )
    consumer.acknowledge.assert_called_once_with("1-0")


def test_other_domain_order_is_ignored_without_matching():
    worker, _, arrival, _ = make_worker(
        domain=MarketFeedDomain.SECURITIES_MARKET
    )

    assert worker.handle_message("1-0", FIELDS) == "acknowledged"
    arrival.match_if_ready.assert_not_called()


def test_historical_terminal_order_is_ignored_during_group_replay():
    worker, _, arrival, execution = make_worker(
        order=make_order(status="FILLED", remaining_volume=0)
    )

    assert worker.handle_message("1-0", FIELDS) == "acknowledged"
    arrival.match_if_ready.assert_not_called()
    execution.execute.assert_not_called()


def test_futures_market_order_uses_market_execution_service():
    worker, _, arrival, execution = make_worker(
        order=make_order(order_type="MARKET")
    )

    assert worker.handle_message("1-0", FIELDS) == "acknowledged"
    execution.execute.assert_called_once_with(order_id="O-1")
    arrival.match_if_ready.assert_not_called()


def test_transient_failure_stays_pending():
    worker, consumer, arrival, _ = make_worker()
    arrival.match_if_ready.side_effect = ConnectionError("redis down")

    assert worker.handle_message("1-0", FIELDS) == "retry"
    consumer.acknowledge.assert_not_called()


def test_invalid_payload_moves_to_dead_letter_at_retry_limit():
    worker, consumer, _, _ = make_worker()
    consumer.increment_failure.return_value = 3

    assert worker.handle_message(
        "1-0", {"event_type": "ORDER_ACCEPTED", "payload": "{"}
    ) == "dead_lettered"
    consumer.publish_dead_letter.assert_called_once()
    consumer.acknowledge.assert_called_once_with("1-0")
