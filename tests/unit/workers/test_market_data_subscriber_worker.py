from contextlib import nullcontext
import logging
from types import SimpleNamespace
from unittest.mock import Mock

from app.infrastructure.market_data.market_tick_store import MarketTickStoreResult
from app.services.market_subscription_service import MarketSubscriptionService
from app.services.market_tick_validation_service import MarketTickValidationError
from app.workers.market_data_subscriber_worker import (
    MarketDataSubscriberWorker,
    QueuedTick,
)
from tests.unit.services.test_market_tick_normalizer import make_data, make_raw, normalize


class MutableClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeSubscription:
    def __init__(self, alive=True):
        self.alive = alive
        self.stop_called = 0
        self.join_called = 0

    def is_alive(self):
        return self.alive

    def stop(self):
        self.stop_called += 1
        self.alive = False

    def join(self, timeout=None):
        self.join_called += 1


def make_worker(*, details=None, queue_size=10, clock=None):
    details = details if details is not None else {}
    index = Mock()
    index.list_all_order_ids.side_effect = lambda: set(details)
    index.get_active_order.side_effect = details.get
    subscription_service = MarketSubscriptionService(
        active_order_index=index,
        debounce_seconds=3,
    )
    feed_client = Mock()
    feed_client.get_latest_ticks.return_value = {}
    feed_client.start_tick_callbacks.side_effect = lambda *args, **kwargs: FakeSubscription()
    market_data_service = Mock()
    tick_store = Mock()
    clock = clock or MutableClock()
    worker = MarketDataSubscriberWorker(
        session_factory=lambda: nullcontext(Mock()),
        feed_client=feed_client,
        market_data_service=market_data_service,
        subscription_service=subscription_service,
        tick_store=tick_store,
        queue_size=queue_size,
        refresh_seconds=1,
        reconnect_initial_seconds=1,
        reconnect_max_seconds=30,
        monotonic=clock,
    )
    return worker, feed_client, market_data_service, subscription_service, clock


def test_on_quote_only_enqueues_tick_messages():
    worker, *_ = make_worker()

    worker.on_quote({}, {"type": "bar"})
    worker.on_quote(make_data(), make_raw())

    stats = worker.stats_snapshot()
    assert stats.received_count == 2
    assert stats.enqueued_count == 1
    assert worker.tick_queue.qsize() == 1


def test_queue_full_never_blocks_and_increments_drop_count():
    worker, *_ = make_worker(queue_size=1)

    worker.on_quote(make_data(), make_raw())
    worker.on_quote(make_data(sequence_id=834), make_raw())

    assert worker.stats_snapshot().queue_full_drop_count == 1


def test_valid_queue_item_updates_publish_counter():
    worker, _feed, market_data_service, *_ = make_worker()
    market_data_service.process_with_session_factory.return_value = SimpleNamespace(
        action=MarketTickStoreResult.PUBLISHED,
        tick=normalize(),
    )

    worker._process_queued_tick(QueuedTick(make_data(), make_raw()))

    stats = worker.stats_snapshot()
    assert stats.processed_count == 1
    assert stats.published_count == 1


def test_duplicate_and_stale_queue_items_update_separate_counters():
    worker, _feed, market_data_service, *_ = make_worker()
    market_data_service.process_with_session_factory.side_effect = [
        SimpleNamespace(action=MarketTickStoreResult.DUPLICATE, tick=normalize()),
        SimpleNamespace(action=MarketTickStoreResult.STALE, tick=normalize()),
    ]

    worker._process_queued_tick(QueuedTick(make_data(), make_raw()))
    worker._process_queued_tick(QueuedTick(make_data(), make_raw()))

    stats = worker.stats_snapshot()
    assert stats.duplicate_count == 1
    assert stats.stale_count == 1


def test_bad_tick_does_not_escape_processing_loop():
    worker, _feed, market_data_service, *_ = make_worker()
    market_data_service.process_with_session_factory.side_effect = (
        MarketTickValidationError("bad")
    )

    worker._process_queued_tick(QueuedTick(make_data(), make_raw()))

    assert worker.stats_snapshot().invalid_count == 1


def test_no_active_orders_does_not_open_empty_subscription():
    worker, feed_client, *_ = make_worker(details={})

    worker.run_once()

    feed_client.start_tick_callbacks.assert_not_called()


def test_new_active_contract_gets_snapshot_then_tick_subscription_after_debounce():
    details = {"O1": {"order_book_id": "AG2609"}}
    worker, feed_client, _service, subscriptions, clock = make_worker(
        details=details
    )
    feed_client.get_latest_ticks.return_value = {"AG2609": None}

    worker.run_once()
    clock.value = 3
    worker.run_once()

    feed_client.get_latest_ticks.assert_called_once_with(frozenset({"AG2609"}))
    feed_client.start_tick_callbacks.assert_called_once()
    assert subscriptions.current_codes == frozenset({"AG2609"})
    assert worker.stats_snapshot().no_tick_count == 1


def test_added_and_removed_contract_rebuilds_subscription():
    details = {"O1": {"order_book_id": "AG2609"}}
    worker, feed_client, _service, subscriptions, clock = make_worker(
        details=details
    )
    worker.run_once()
    clock.value = 3
    worker.run_once()
    first_subscription = worker._subscription

    details["O2"] = {"order_book_id": "AU2608"}
    clock.value = 4
    worker.run_once()
    clock.value = 7
    worker.run_once()

    assert first_subscription.stop_called == 1
    assert subscriptions.current_codes == frozenset({"AG2609", "AU2608"})
    assert feed_client.start_tick_callbacks.call_count == 2

    details.clear()
    clock.value = 8
    worker.run_once()
    clock.value = 11
    worker.run_once()
    assert subscriptions.current_codes == frozenset()
    assert worker._subscription is None


def test_subscription_receipt_idle_is_not_error():
    worker, *_ = make_worker()

    worker.on_subscribe(
        {
            "contracts": {
                "AG2609": {
                    "exists": True,
                    "is_live": True,
                    "subscribed": True,
                    "session_state": "idle",
                }
            }
        }
    )

    assert worker.last_error == ""


def test_subscription_receipt_records_contract_and_subscription_failures(caplog):
    worker, *_ = make_worker()

    with caplog.at_level(logging.WARNING):
        worker.on_subscribe(
            {
                "contracts": {
                    "MISSING": {
                        "exists": False,
                        "is_live": False,
                        "subscribed": False,
                        "session_state": "unknown",
                    },
                    "FAILED": {
                        "exists": True,
                        "is_live": True,
                        "subscribed": False,
                        "session_state": "active",
                    },
                }
            }
        )

    assert "CONTRACT_NOT_FOUND" in caplog.text
    assert "SUBSCRIBE_FAILED" in caplog.text


def test_ingestion_stopped_is_not_recorded_as_failure():
    worker, *_ = make_worker()

    worker.on_message({"type": "status", "code": "INGESTION_STOPPED"})

    assert worker.last_error == ""


def test_dead_subscription_reconnects_after_exponential_delay():
    details = {"O1": {"order_book_id": "AG2609"}}
    worker, feed_client, _service, _subscriptions, clock = make_worker(
        details=details
    )
    worker.run_once()
    clock.value = 3
    worker.run_once()
    worker._subscription.alive = False

    clock.value = 4
    worker.run_once()
    assert feed_client.start_tick_callbacks.call_count == 1
    clock.value = 5
    worker.run_once()

    assert feed_client.start_tick_callbacks.call_count == 2
    assert worker.stats_snapshot().reconnect_count == 1


def test_request_stop_and_shutdown_stop_subscription_and_consumer_thread():
    worker, *_ = make_worker()
    subscription = FakeSubscription()
    worker._subscription = subscription

    worker.request_stop()
    worker.shutdown()

    assert worker.stop_event.is_set()
    assert subscription.stop_called == 1
    assert subscription.join_called == 1
