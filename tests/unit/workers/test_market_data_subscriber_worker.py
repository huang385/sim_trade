from contextlib import nullcontext
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from app.schemas.market_tick_schema import MarketTickIngestType
from app.services.market_data_service import MarketDataProcessAction
from app.services.market_data_code_mapping_service import (
    MarketDataCodeMappingSnapshot,
)
from app.services.market_subscription_service import MarketSubscriptionService
from app.services.market_tick_validation_service import MarketTickValidationError
from app.workers.market_data_subscriber_worker import (
    MarketDataSourceStatus,
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


def make_worker(
    *,
    details=None,
    queue_size=10,
    clock=None,
    shutdown_drain_timeout_seconds=10,
):
    details = details if details is not None else {}
    index = Mock()
    index.list_all_order_ids.side_effect = lambda: set(details)
    index.get_active_order.side_effect = details.get
    index.list_active_contract_codes.side_effect = lambda: {
        detail["order_book_id"]
        for detail in details.values()
        if detail.get("order_book_id")
    }
    active_position_source = Mock()
    active_position_source.list_active_contract_codes.return_value = set()
    subscription_service = MarketSubscriptionService(
        active_order_index=index,
        active_position_contract_source=active_position_source,
        debounce_seconds=3,
    )
    feed_client = Mock()
    feed_client.get_latest_ticks.return_value = {}
    feed_client.start_tick_callbacks.side_effect = lambda *args, **kwargs: FakeSubscription()
    feed_client.replace_tick_subscriptions.side_effect = lambda codes: {
        "contracts": {
            code: {"exists": True, "is_live": True, "subscribed": True}
            for code in codes
        }
    }
    market_data_service = Mock()
    code_mapping_service = Mock()
    code_mapping_service.build_snapshot.side_effect = (
        lambda _db, codes: MarketDataCodeMappingSnapshot.identity(codes)
    )
    tick_store = Mock()
    clock = clock or MutableClock()
    worker = MarketDataSubscriberWorker(
        session_factory=lambda: nullcontext(Mock()),
        feed_client=feed_client,
        market_data_service=market_data_service,
        code_mapping_service=code_mapping_service,
        subscription_service=subscription_service,
        tick_store=tick_store,
        queue_size=queue_size,
        refresh_seconds=1,
        reconnect_initial_seconds=1,
        reconnect_max_seconds=30,
        shutdown_drain_timeout_seconds=shutdown_drain_timeout_seconds,
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
    assert worker.last_tick_at is not None


def test_queue_full_never_blocks_and_increments_drop_count():
    worker, *_ = make_worker(queue_size=1)

    worker.on_quote(make_data(), make_raw())
    worker.on_quote(make_data(sequence_id=834), make_raw())

    assert worker.stats_snapshot().queue_full_drop_count == 1


def test_valid_queue_item_updates_publish_counter():
    worker, _feed, market_data_service, *_ = make_worker()
    market_data_service.process_with_session_factory.return_value = SimpleNamespace(
        action=MarketDataProcessAction.PUBLISHED,
        tick=normalize(),
    )

    worker._process_queued_tick(QueuedTick(make_data(), make_raw()))

    stats = worker.stats_snapshot()
    assert stats.processed_count == 1
    assert stats.published_count == 1
    assert worker.last_published_at is not None


def test_current_subscription_generation_is_forwarded_to_atomic_publish():
    worker, _feed, market_data_service, *_ = make_worker()
    tick = normalize()
    market_data_service.process_with_session_factory.return_value = (
        SimpleNamespace(
            action=MarketDataProcessAction.PUBLISHED,
            tick=tick,
        )
    )

    worker._process_queued_tick(
        QueuedTick(
            make_data(),
            make_raw(),
            subscription_generation=7,
        )
    )

    market_data_service.process_with_session_factory.assert_called_once_with(
        worker.session_factory,
        data=make_data(),
        raw=make_raw(),
        ingest_type=MarketTickIngestType.LIVE_CALLBACK,
        subscription_generation=7,
    )


def test_websocket_callback_captures_subscription_generation():
    worker, *_ = make_worker()

    worker.on_quote(make_data(), make_raw(), generation=9)

    queued = worker.tick_queue.get_nowait()
    assert queued.subscription_generation == 9


def test_bad_tick_does_not_escape_processing_loop():
    worker, _feed, market_data_service, *_ = make_worker()
    market_data_service.process_with_session_factory.side_effect = (
        MarketTickValidationError("bad")
    )

    worker._process_queued_tick(QueuedTick(make_data(), make_raw()))

    assert worker.stats_snapshot().invalid_count == 1


def test_invalid_tick_detail_log_is_rate_limited(caplog):
    """连续坏 Tick 继续计数，但诊断日志最多每五秒输出一次。"""

    clock = MutableClock(10)
    worker, *_ = make_worker(clock=clock)

    with caplog.at_level(logging.WARNING):
        worker._record_invalid_tick(ValueError("first detail"), "JD2609")
        clock.value = 12
        worker._record_invalid_tick(ValueError("second detail"), "JD2609")
        clock.value = 15
        worker._record_invalid_tick(ValueError("third detail"), "JD2609")

    assert worker.stats_snapshot().invalid_count == 3
    assert caplog.text.count("行情Tick校验失败") == 2
    assert "first detail" in caplog.text
    assert "second detail" not in caplog.text
    assert "third detail" in caplog.text


def test_storage_slow_consumer_does_not_mark_live_subscription_failed(caplog):
    """行情中心存储积压与本进程实时回调无关，只记录告警。"""

    worker, *_ = make_worker()

    with caplog.at_level(logging.WARNING):
        worker.on_error({"raw": {"code": "STORAGE_SLOW_CONSUMER"}})

    assert worker.last_error == ""
    assert "实时订阅继续运行" in caplog.text


def test_storage_slow_consumer_warning_is_deduplicated_for_sixty_seconds(
    caplog,
):
    clock = MutableClock(10)
    worker, *_ = make_worker(clock=clock)

    with caplog.at_level(logging.WARNING):
        worker.on_message(
            {
                "type": "status",
                "component": "storage",
                "state": "slow_consumer",
            }
        )
        worker.on_error({"raw": {"code": "STORAGE_SLOW_CONSUMER"}})
        clock.value = 69
        worker.on_message(
            {
                "type": "status",
                "component": "storage",
                "state": "slow_consumer",
            }
        )
        clock.value = 70
        worker.on_message(
            {
                "type": "status",
                "component": "storage",
                "state": "slow_consumer",
            }
        )

    assert caplog.text.count("行情中心存储消费较慢") == 2
    assert worker.last_error == ""


def test_no_active_orders_does_not_open_empty_subscription():
    worker, feed_client, *_ = make_worker(details={})

    worker.run_once()

    feed_client.start_tick_callbacks.assert_not_called()


def test_new_active_contract_starts_websocket_after_debounce_without_rest():
    details = {"O1": {"order_book_id": "AG2609"}}
    worker, feed_client, _service, subscriptions, clock = make_worker(
        details=details
    )
    worker.run_once()
    clock.value = 3
    worker.run_once()

    feed_client.get_latest_ticks.assert_not_called()
    feed_client.start_tick_callbacks.assert_called_once()
    assert subscriptions.current_codes == frozenset({"AG2609"})


def test_option_subscription_uses_source_code_and_callback_restores_internal_code():
    details = {"O1": {"order_book_id": "JD2609-C-4000"}}
    worker, feed_client, _service, subscriptions, clock = make_worker(
        details=details
    )
    mapping = MarketDataCodeMappingSnapshot(
        internal_to_source={"JD2609-C-4000": "JD2609C4000"},
        source_to_internal={"JD2609C4000": "JD2609-C-4000"},
    )
    worker.code_mapping_service.build_snapshot.return_value = mapping
    worker.code_mapping_service.build_snapshot.side_effect = None

    worker.run_once()
    clock.value = 3
    worker.run_once()

    call = feed_client.start_tick_callbacks.call_args
    assert call.args[0] == frozenset({"JD2609C4000"})
    call.kwargs["on_subscribe"](
        {
            "contracts": {
                "JD2609C4000": {
                    "exists": True,
                    "is_live": True,
                    "subscribed": True,
                }
            }
        }
    )
    assert subscriptions.state_snapshot().subscribed_codes == frozenset(
        {"JD2609-C-4000"}
    )

    option_data = make_data(code="JD2609C4000")
    call.kwargs["on_quote"](option_data, make_raw())
    queued = worker.tick_queue.get_nowait()
    assert queued.data["order_book_id"] == "JD2609-C-4000"


def test_code_mapping_is_built_once_per_subscription_not_per_tick():
    details = {"O1": {"order_book_id": "JD2609-C-4000"}}
    worker, feed_client, _service, _subscriptions, clock = make_worker(
        details=details
    )
    mapping = MarketDataCodeMappingSnapshot(
        internal_to_source={"JD2609-C-4000": "JD2609C4000"},
        source_to_internal={"JD2609C4000": "JD2609-C-4000"},
    )
    worker.code_mapping_service.build_snapshot.return_value = mapping
    worker.code_mapping_service.build_snapshot.side_effect = None

    worker.run_once()
    clock.value = 3
    worker.run_once()
    quote_callback = feed_client.start_tick_callbacks.call_args.kwargs["on_quote"]
    for sequence_id in range(100):
        quote_callback(
            make_data(code="JD2609C4000", sequence_id=sequence_id),
            make_raw(),
        )

    worker.code_mapping_service.build_snapshot.assert_called_once()


def test_closed_sdk_connection_late_callback_cannot_enter_new_generation():
    """旧SDK线程关闭边界的晚到回调不能污染新连接的行情队列。"""

    worker, feed_client, *_ = make_worker()
    callbacks = []

    def capture_callbacks(*_args, **kwargs):
        callbacks.append(kwargs["on_quote"])
        return FakeSubscription()

    feed_client.start_tick_callbacks.side_effect = capture_callbacks
    worker._start_subscription(frozenset({"AG2609"}))
    old_callback = callbacks[-1]
    worker._stop_subscription()
    worker._start_subscription(frozenset({"AG2609"}))
    current_callback = callbacks[-1]

    old_callback(make_data(sequence_id=100), make_raw())
    current_callback(make_data(sequence_id=101), make_raw())

    queued = worker.tick_queue.get_nowait()
    assert queued.data["sequence_id"] == 101
    assert worker.tick_queue.empty()


def test_added_and_removed_contract_updates_existing_sdk_subscription():
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

    assert first_subscription.stop_called == 0
    assert subscriptions.current_codes == frozenset({"AG2609", "AU2608"})
    assert feed_client.start_tick_callbacks.call_count == 1
    feed_client.replace_tick_subscriptions.assert_called_once_with(
        frozenset({"AG2609", "AU2608"})
    )

    details.clear()
    clock.value = 8
    worker.run_once()
    clock.value = 11
    worker.run_once()
    assert subscriptions.current_codes == frozenset()
    assert worker._subscription is None
    assert first_subscription.stop_called == 1


def test_subscription_receipt_idle_is_not_error():
    worker, _feed, _service, subscriptions, _clock = make_worker()
    subscriptions.mark_requested(frozenset({"AG2609"}))

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
    worker, _feed, _service, subscriptions, _clock = make_worker()
    subscriptions.mark_requested(frozenset({"MISSING", "FAILED"}))

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


def test_partial_failure_is_degraded_and_retried_after_backoff():
    details = {
        "O1": {"order_book_id": "AG2609"},
        "O2": {"order_book_id": "AU2608"},
    }
    worker, feed_client, _service, subscriptions, clock = make_worker(details=details)
    worker.run_once()
    clock.value = 3
    worker.run_once()
    callback = feed_client.start_tick_callbacks.call_args.kwargs["on_subscribe"]
    callback(
        {
            "contracts": {
                "AG2609": {"exists": True, "is_live": True, "subscribed": True},
                "AU2608": {"exists": True, "is_live": True, "subscribed": False},
            }
        }
    )

    worker._publish_source_status()
    mapping = worker.tick_store.update_source_status.call_args.args[0]
    assert subscriptions.state_snapshot().failed_codes == frozenset({"AU2608"})
    assert mapping["status"] == MarketDataSourceStatus.DEGRADED.value

    clock.value = 3.9
    worker.run_once()
    assert feed_client.start_tick_callbacks.call_count == 1
    clock.value = 4
    worker.run_once()
    feed_client.replace_tick_subscriptions.assert_called_once_with(
        frozenset({"AG2609", "AU2608"})
    )
    assert feed_client.start_tick_callbacks.call_count == 1


def test_all_confirmed_subscription_is_running():
    details = {"O1": {"order_book_id": "AG2609"}}
    worker, feed_client, *_rest, clock = make_worker(details=details)
    worker.run_once()
    clock.value = 3
    worker.run_once()
    callback = feed_client.start_tick_callbacks.call_args.kwargs["on_subscribe"]
    callback(
        {
            "contracts": {
                "AG2609": {"exists": True, "is_live": True, "subscribed": True}
            }
        }
    )

    worker._publish_source_status()
    mapping = worker.tick_store.update_source_status.call_args.args[0]
    assert mapping["status"] == MarketDataSourceStatus.RUNNING.value
    assert mapping["subscription_generation"] == 1
    assert mapping["last_successful_subscribe_at"] is not None


def test_idle_and_disconnected_statuses_are_published():
    worker, _feed, *_ = make_worker(details={})
    worker.run_once()
    mapping = worker.tick_store.update_source_status.call_args.args[0]
    assert mapping["status"] == MarketDataSourceStatus.IDLE.value

    details = {"O1": {"order_book_id": "AG2609"}}
    worker, _feed, _service, _subscriptions, clock = make_worker(details=details)
    worker.run_once()
    clock.value = 3
    worker.run_once()
    worker._subscription.alive = False
    clock.value = 4
    worker.run_once()
    mapping = worker.tick_store.update_source_status.call_args.args[0]
    assert mapping["status"] == MarketDataSourceStatus.DISCONNECTED.value


def test_status_hash_is_sorted_and_contains_no_sensitive_fields():
    worker, _feed, _service, subscriptions, _clock = make_worker()
    subscriptions.mark_requested(frozenset({"ZZ", "AA"}))
    worker._publish_source_status()

    mapping = worker.tick_store.update_source_status.call_args.args[0]
    assert mapping["requested_codes"] == "AA,ZZ"
    assert mapping["queue_capacity"] == 10
    assert not any(
        word in key.lower()
        for key in mapping
        for word in ("token", "password", "api_user", "url")
    )


def test_temporary_redis_status_failure_does_not_break_next_worker_cycle():
    worker, _feed, *_ = make_worker(details={})
    worker.tick_store.update_source_status.side_effect = [RuntimeError("redis"), None]

    worker.run_once()
    worker.run_once()

    assert worker.tick_store.update_source_status.call_count == 2


def test_normal_component_status_is_not_recorded_as_failure():
    worker, *_ = make_worker()

    worker.on_message(
        {"type": "status", "component": "storage", "state": "connected"}
    )

    assert worker.last_error == ""


def test_sdk_reconnect_status_does_not_start_competing_reconnect_loop():
    worker, _feed, _service, subscriptions, _clock = make_worker()
    subscription = FakeSubscription()
    worker._subscription = subscription
    worker._desired_codes = frozenset({"AG2609"})
    subscriptions.mark_requested(frozenset({"AG2609"}))

    worker.on_message(
        {"type": "status", "component": "hub", "state": "reconnecting"}
    )
    assert worker._derive_status(0) == MarketDataSourceStatus.DISCONNECTED
    assert subscription.stop_called == 0

    worker.on_message(
        {"type": "status", "component": "hub", "state": "recovered"}
    )
    assert worker._source_issue == ""


def test_slow_consumer_gap_stays_degraded_after_recovery():
    worker, _feed, _service, subscriptions, _clock = make_worker()
    worker._desired_codes = frozenset({"AG2609"})
    subscriptions.mark_requested(frozenset({"AG2609"}))
    worker.on_message(
        {
            "type": "status",
            "component": "session",
            "state": "slow_consumer",
        }
    )
    worker.on_message(
        {"type": "status", "component": "session", "state": "recovered"}
    )

    assert worker._derive_status(0) == MarketDataSourceStatus.DEGRADED


def test_storage_slow_consumer_does_not_degrade_live_session():
    worker, _feed, _service, subscriptions, _clock = make_worker()
    worker._desired_codes = frozenset({"JD2609"})
    subscriptions.mark_requested(frozenset({"JD2609"}))
    previous_status = worker._derive_status(0)

    worker.on_message(
        {
            "type": "status",
            "component": "storage",
            "state": "slow_consumer",
        }
    )

    assert worker._source_issue == ""
    assert worker._derive_status(0) == previous_status


def test_replaced_sdk_session_requests_worker_stop():
    worker, *_ = make_worker()

    worker.on_message(
        {"type": "status", "component": "session", "state": "replaced"}
    )

    assert worker.stop_event.is_set()
    assert worker.last_error == "SESSION_REPLACED"


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
    mapping = worker.tick_store.update_source_status.call_args.args[0]
    assert mapping["status"] == MarketDataSourceStatus.STOPPED.value


def test_shutdown_drains_queue_and_consumer_thread_exits():
    worker, _feed, market_data_service, *_ = make_worker()
    market_data_service.process_with_session_factory.return_value = SimpleNamespace(
        action=MarketDataProcessAction.PUBLISHED,
        tick=normalize(),
    )
    worker.start_consumer_thread()
    worker.on_quote(make_data(), make_raw())

    worker.shutdown()

    assert worker.tick_queue.empty()
    assert not worker._consumer_thread.is_alive()
    assert worker.stats_snapshot().shutdown_drop_count == 0


def test_shutdown_timeout_drops_remaining_queue_and_joins_consumer():
    worker, _feed, market_data_service, *_ = make_worker(
        shutdown_drain_timeout_seconds=0.01
    )
    entered = threading.Event()
    release = threading.Event()

    def slow_process(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=2)
        return SimpleNamespace(
            action=MarketDataProcessAction.PUBLISHED,
            tick=normalize(),
        )

    market_data_service.process_with_session_factory.side_effect = slow_process
    worker.start_consumer_thread()
    worker.on_quote(make_data(), make_raw())
    assert entered.wait(timeout=1)
    worker.on_quote(make_data(sequence_id=834), make_raw())
    shutdown_thread = threading.Thread(target=worker.shutdown)
    shutdown_thread.start()
    time.sleep(0.05)
    release.set()
    shutdown_thread.join(timeout=2)

    assert not shutdown_thread.is_alive()
    assert not worker._consumer_thread.is_alive()
    assert worker.stats_snapshot().shutdown_drop_count == 1
