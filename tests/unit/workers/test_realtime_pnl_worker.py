import json
from types import MappingProxyType

from redis.exceptions import ConnectionError

from app.schemas.market_tick_schema import MarketTick
from app.services.active_position_cache import ActivePositionCycleSnapshot
from app.services.realtime_pnl_service import (
    PnlWorkerLeaseLostError,
    RealtimePnlProcessResult,
    RealtimePnlService,
)
from app.workers.realtime_pnl_worker import RealtimePnlWorker


def make_fields(symbol: str, price: str, sequence: int) -> dict[str, str]:
    return {
        "event_type": "MARKET_TICK",
        "payload": json.dumps(
            {
                "source_event_id": f"{symbol}-{sequence}",
                "source": "YML_FEEDHUB",
                "ingest_type": "LIVE_CALLBACK",
                "order_book_id": symbol,
                "exchange_id": "DCE",
                "symbol": symbol,
                "trading_day": "2026-07-28",
                "event_time": "2026-07-28T10:00:00+08:00",
                "sequence_id": sequence,
                "last_price": price,
                "cumulative_volume": sequence,
                "bid_volume_1": 1,
                "ask_volume_1": 1,
            }
        ),
    }


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class FakeConsumer:
    stream_name = "stream:test"
    group_name = "group:test"
    consumer_name = "consumer:test"

    def __init__(self, messages):
        self.messages = list(messages)
        self.acked_batches = []
        self.block_values = []
        self.dead_letters = []

    def claim_stale_messages(self, **_kwargs):
        return []

    def read_new_messages(self, *, batch_size, block_ms):
        self.block_values.append(block_ms)
        rows, self.messages = (
            self.messages[:batch_size],
            self.messages[batch_size:],
        )
        return rows

    def acknowledge_many(self, message_ids):
        self.acked_batches.append(list(message_ids))
        return len(message_ids)

    def clear_failures(self, _message_ids):
        return None

    def acknowledge(self, message_id):
        self.acked_batches.append([message_id])
        return 1

    def clear_failure(self, _message_id):
        return None

    def publish_dead_letter(self, **kwargs):
        self.dead_letters.append(kwargs)

    def increment_failure(self, _message_id):
        return 1


class FakeCache:
    def __init__(self):
        self.calls = 0
        self.call_kwargs = []

    def get_cycle_snapshot(self, **kwargs):
        self.calls += 1
        self.call_kwargs.append(kwargs)
        return ActivePositionCycleSnapshot(
            by_contract=MappingProxyType({}),
            by_account=MappingProxyType({}),
            accounts=MappingProxyType({}),
            cache_version="1",
            refresh_count=1,
        )


class FakePnlStore:
    def __init__(self):
        self.dirty = []
        self.account_dirty = []
        self.completed = []
        self.completed_accounts = []
        self.lease_valid = True
        self.acquire_calls = 0
        self.renew_calls = 0
        self.release_calls = 0
        self.rebuild_calls = 0
        self.account_fact_marks = []

    def acquire_worker_lease(self, _owner, _ttl_seconds):
        self.acquire_calls += 1
        return self.lease_valid

    def list_dirty_contracts(self):
        return list(self.dirty)

    def list_dirty_account_facts(self):
        return list(self.account_dirty)

    def complete_dirty_contract(self, **kwargs):
        self.completed.append(kwargs)
        return True

    def complete_dirty_account_fact(self, account_id, expected_version):
        self.completed_accounts.append((account_id, expected_version))
        return True

    def mark_account_fact_dirty_once(
        self,
        *,
        event_id,
        account_id,
        processed_ttl_seconds,
    ):
        self.account_fact_marks.append(
            (event_id, account_id, processed_ttl_seconds)
        )
        return "1"

    def rebuild_active_indexes(self, **_kwargs):
        self.rebuild_calls += 1
        return True

    def renew_worker_lease(self, _owner, _ttl_seconds):
        self.renew_calls += 1
        return self.lease_valid

    def release_worker_lease(self, _owner):
        self.release_calls += 1
        return self.lease_valid


class FakeMarketStore:
    def __init__(self):
        self.latest = {}

    def get_latest_many(self, contract_keys):
        return {
            key: self.latest.get(key, {})
            for key in sorted(set(contract_keys))
        }


class FakeService:
    parse_tick = staticmethod(RealtimePnlService.parse_tick)

    def __init__(self, store, *, fail=False, successful=None):
        self.pnl_store = store
        self.active_position_cache = FakeCache()
        self.calls = []
        self.fail = fail
        self.successful = successful

    def process_batch(self, *, requests, **_kwargs):
        self.calls.append(requests)
        if self.fail:
            raise RuntimeError("redis unavailable")
        successful = (
            set(self.successful)
            if self.successful is not None
            else {item.key for item in requests}
        )
        failed = {item.key for item in requests} - successful
        return RealtimePnlProcessResult(
            action="CALCULATED",
            successful_contracts=frozenset(successful),
            failed_contracts=frozenset(failed),
            redis_snapshots_written=len(successful),
        )


def make_worker(messages, *, service=None, store=None, market_store=None):
    clock = Clock()
    store = store or FakePnlStore()
    service = service or FakeService(store)
    consumer = FakeConsumer(messages)
    worker = RealtimePnlWorker(
        stream_consumer=consumer,
        service=service,
        pnl_store=store,
        market_tick_store=market_store or FakeMarketStore(),
        batch_size=2000,
        block_ms=5000,
        pending_idle_ms=60000,
        max_retries=3,
        retry_interval_seconds=0,
        calculation_interval_ms=500,
        monotonic=clock,
    )
    return worker, consumer, service, store, clock


def test_four_ticks_in_window_use_latest_price_and_ack_all_once():
    messages = [
        (f"{index}-0", make_fields("JD2609", str(3200 + index), index))
        for index in range(1, 5)
    ]
    worker, consumer, service, _store, _clock = make_worker(messages)

    worker.run_once(force_flush=True)

    assert len(service.calls) == 1
    assert service.calls[0][0].tick.last_price == 3204
    assert consumer.acked_batches == [["1-0", "2-0", "3-0", "4-0"]]
    assert worker.stats_snapshot().ticks_coalesced == 3


def test_two_contracts_keep_their_own_latest_tick():
    messages = [
        ("1-0", make_fields("JD2609", "3200", 1)),
        ("2-0", make_fields("AG2612", "14000", 2)),
        ("3-0", make_fields("JD2609", "3210", 3)),
    ]
    worker, _consumer, service, _store, _clock = make_worker(messages)

    worker.run_once(force_flush=True)

    prices = {item.key: item.tick.last_price for item in service.calls[0]}
    assert prices == {
        ("DCE", "AG2612"): 14000,
        ("DCE", "JD2609"): 3210,
    }


def test_same_price_without_dirty_is_acked_without_second_calculation():
    worker, consumer, service, _store, _clock = make_worker(
        [("1-0", make_fields("JD2609", "3200", 1))]
    )
    worker.run_once(force_flush=True)
    consumer.messages.append(
        ("2-0", make_fields("JD2609", "3200", 2))
    )

    worker.run_once(force_flush=True)

    assert len(service.calls) == 1
    assert consumer.acked_batches[-1] == ["2-0"]
    assert worker.stats_snapshot().contracts_skipped_unchanged == 1


def test_same_price_with_trade_dirty_is_recalculated():
    store = FakePnlStore()
    worker, consumer, service, _store, _clock = make_worker(
        [("1-0", make_fields("JD2609", "3200", 1))],
        store=store,
    )
    worker.run_once(force_flush=True)
    store.dirty = [(("DCE", "JD2609"), "9", {"A001"})]

    worker.run_once(force_flush=True)

    assert len(service.calls) == 2
    assert store.completed[0]["expected_version"] == "9"


def test_redis_failure_keeps_all_messages_pending():
    store = FakePnlStore()
    service = FakeService(store, fail=True)
    worker, consumer, _service, _store, _clock = make_worker(
        [
            ("1-0", make_fields("JD2609", "3200", 1)),
            ("2-0", make_fields("JD2609", "3201", 2)),
        ],
        service=service,
        store=store,
    )

    worker.run_once(force_flush=True)

    assert consumer.acked_batches == []
    assert ("DCE", "JD2609") in worker._buffer


def test_partial_contract_failure_only_acks_successful_contract():
    store = FakePnlStore()
    service = FakeService(
        store,
        successful={("DCE", "JD2609")},
    )
    worker, consumer, _service, _store, _clock = make_worker(
        [
            ("1-0", make_fields("JD2609", "3200", 1)),
            ("2-0", make_fields("AG2612", "14000", 2)),
        ],
        service=service,
        store=store,
    )

    worker.run_once(force_flush=True)

    assert consumer.acked_batches == [["1-0"]]
    assert ("DCE", "AG2612") in worker._buffer


def test_pending_trigger_uses_current_latest_market_not_old_price():
    market = FakeMarketStore()
    current = MarketTick.model_validate(
        json.loads(make_fields("JD2609", "3250", 2)["payload"])
    )
    market.latest[("DCE", "JD2609")] = current.model_dump(
        mode="json"
    )
    worker, _consumer, service, _store, _clock = make_worker(
        [("1-0", make_fields("JD2609", "3100", 1))],
        market_store=market,
    )

    worker.run_once(force_flush=True)

    assert service.calls[0][0].tick.last_price == 3250


def test_one_thousand_ticks_are_coalesced_to_one_calculation():
    messages = [
        (
            f"{index}-0",
            make_fields("JD2609", str(3200 + index), index),
        )
        for index in range(1, 1001)
    ]
    worker, consumer, service, _store, _clock = make_worker(messages)

    worker.run_once(force_flush=True)

    assert len(service.calls) == 1
    assert len(service.calls[0]) == 1
    assert len(consumer.acked_batches[0]) == 1000
    assert worker.stats_snapshot().ticks_coalesced == 999


def test_dynamic_block_does_not_exceed_remaining_flush_time():
    worker, consumer, _service, _store, clock = make_worker([])
    clock.value = 0.35

    worker.run_once()

    assert 149 <= consumer.block_values[0] <= 150


def test_lost_lease_does_not_read_or_ack_messages():
    store = FakePnlStore()
    store.lease_valid = False
    worker, consumer, service, _store, _clock = make_worker(
        [("1-0", make_fields("JD2609", "3200", 1))],
        store=store,
    )

    worker.run_once(force_flush=True)

    assert consumer.acked_batches == []
    assert consumer.messages
    assert service.calls == []


def test_lease_lost_during_calculation_keeps_buffer_pending():
    store = FakePnlStore()

    class LeaseLosingService(FakeService):
        def process_batch(self, *, requests, **_kwargs):
            self.calls.append(requests)
            self.pnl_store.lease_valid = False
            raise PnlWorkerLeaseLostError("lease expired")

    service = LeaseLosingService(store)
    worker, consumer, _service, _store, _clock = make_worker(
        [("1-0", make_fields("JD2609", "3200", 1))],
        service=service,
        store=store,
    )

    worker.run_once(force_flush=True)

    assert consumer.acked_batches == []
    assert ("DCE", "JD2609") in worker._buffer


def test_failed_dirty_does_not_force_full_cache_refresh_every_cycle():
    store = FakePnlStore()
    store.dirty = [(("DCE", "JD2609"), "9", {"A001"})]
    service = FakeService(store, successful=set())
    worker, _consumer, _service, _store, clock = make_worker(
        [],
        service=service,
        store=store,
    )
    worker._next_reconciliation_at = 999

    worker.run_once(force_flush=True)
    clock.value = 0.5
    worker.run_once(force_flush=True)

    assert service.active_position_cache.calls == 2
    assert all(
        call["force_refresh"] is False
        for call in service.active_position_cache.call_kwargs
    )


def test_lease_is_renewed_only_when_five_second_deadline_is_reached():
    worker, _consumer, _service, store, clock = make_worker([])
    worker._next_reconciliation_at = 999

    worker.run_once(force_flush=True)
    for value in (0.5, 1.0, 2.5, 4.99):
        clock.value = value
        worker.run_once(force_flush=True)

    assert store.acquire_calls == 1
    assert store.renew_calls == 0

    clock.value = 5.0
    worker.run_once(force_flush=True)

    assert store.renew_calls == 1
    assert worker.stats_snapshot().lease_renew_count == 1


def test_account_fact_dirty_is_processed_without_contract_request():
    store = FakePnlStore()
    store.account_dirty = [("A001", "8")]

    class AccountFactService(FakeService):
        def process_batch(
            self,
            *,
            requests,
            account_fact_versions,
            **_kwargs,
        ):
            self.calls.append(requests)
            assert requests == []
            assert account_fact_versions == {"A001": "8"}
            return RealtimePnlProcessResult(
                action="CALCULATED",
                accounts_updated=1,
                redis_snapshots_written=1,
                successful_account_facts=frozenset({"A001"}),
            )

    service = AccountFactService(store)
    worker, _consumer, _service, _store, _clock = make_worker(
        [],
        service=service,
        store=store,
    )

    worker.run_once(force_flush=True)

    assert store.completed_accounts == [("A001", "8")]


def test_write_fence_rejection_marks_worker_lost_and_reacquires_later():
    store = FakePnlStore()

    class RejectingService(FakeService):
        def process_batch(self, *, requests, **_kwargs):
            self.calls.append(requests)
            raise PnlWorkerLeaseLostError("lease rejected")

    service = RejectingService(store)
    worker, consumer, _service, _store, _clock = make_worker(
        [("1-0", make_fields("JD2609", "3200", 1))],
        service=service,
        store=store,
    )

    worker.run_once(force_flush=True)

    assert worker._lease_acquired is False
    assert worker.stats_snapshot().lease_lost_count == 1
    assert worker.stats_snapshot().lease_write_rejected_count == 1
    assert consumer.acked_batches == []

    store.lease_valid = False
    worker.run_once(force_flush=True)
    assert store.acquire_calls == 2
    assert consumer.acked_batches == []


def test_redis_write_exception_fails_closed_without_dead_letter_or_ack():
    store = FakePnlStore()

    class RedisFailingService(FakeService):
        def process_batch(self, *, requests, **_kwargs):
            self.calls.append(requests)
            raise ConnectionError("redis unavailable")

    service = RedisFailingService(store)
    worker, consumer, _service, _store, _clock = make_worker(
        [("1-0", make_fields("JD2609", "3200", 1))],
        service=service,
        store=store,
    )

    worker.run_once(force_flush=True)

    assert worker._lease_acquired is False
    assert consumer.acked_batches == []
    assert consumer.dead_letters == []
    assert ("DCE", "JD2609") in worker._buffer


def test_account_fact_redis_write_failure_does_not_clear_dirty():
    store = FakePnlStore()
    store.account_dirty = [("A001", "9")]

    class RedisFailingAccountService(FakeService):
        def process_batch(self, **_kwargs):
            raise ConnectionError("redis unavailable")

    worker, consumer, _service, _store, _clock = make_worker(
        [],
        service=RedisFailingAccountService(store),
        store=store,
    )

    worker.run_once(force_flush=True)

    assert store.completed_accounts == []
    assert store.account_dirty == [("A001", "9")]
    assert consumer.acked_batches == []
    assert worker._lease_acquired is False


def test_option_margin_adjustment_marks_account_fact_dirty_before_ack():
    store = FakePnlStore()

    class MarginService(FakeService):
        def process_batch(self, *, requests, **_kwargs):
            self.calls.append(requests)
            return RealtimePnlProcessResult(
                action="CALCULATED",
                successful_contracts=frozenset({("DCE", "JD2609")}),
                margin_adjustment_positions=(
                    ("A001", "P001", ("DCE", "JD2609")),
                ),
            )

    class AdjustmentService:
        def __init__(self):
            self.calls = []

        def adjust(self, db, *, account_id, position_id):
            self.calls.append((db, account_id, position_id))

    class SessionContext:
        def __enter__(self):
            return "DB"

        def __exit__(self, *_args):
            return False

    adjustment = AdjustmentService()
    service = MarginService(store)
    worker, consumer, _service, _store, _clock = make_worker(
        [("1-0", make_fields("JD2609", "3200", 1))],
        service=service,
        store=store,
    )
    worker.option_margin_adjustment_service = adjustment
    worker.session_factory = SessionContext

    worker.run_once(force_flush=True)

    assert adjustment.calls == [("DB", "A001", "P001")]
    assert store.account_fact_marks == [
        (
            "OPTION_MARGIN_ADJUSTED:JD2609-1:P001",
            "A001",
            604800,
        )
    ]
    assert consumer.acked_batches == [["1-0"]]


def test_active_indexes_are_reconciled_again_on_periodic_full_cycle():
    worker, _consumer, _service, store, clock = make_worker([])

    worker.run_once()
    assert store.rebuild_calls == 1

    clock.value = 60
    worker.run_once()
    assert store.rebuild_calls == 2
