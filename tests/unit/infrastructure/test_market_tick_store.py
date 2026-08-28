import json
from unittest.mock import Mock

from app.infrastructure.market_data.market_tick_store import (
    MarketTickStore,
    MarketTickStoreResult,
)
from tests.unit.services.test_market_tick_normalizer import normalize


def test_publish_passes_decimal_strings_and_returns_result():
    redis_client = Mock()
    redis_client.eval.return_value = "PUBLISHED"
    store = MarketTickStore(
        redis_client,
        stream_name="stream:test-market",
    )
    tick = normalize()

    result = store.publish(tick)

    assert result == MarketTickStoreResult.PUBLISHED
    arguments = redis_client.eval.call_args.args
    payload = json.loads(arguments[9])
    assert payload["last_price"] == "14600.0"
    assert payload["cumulative_turnover"] == "5298353100.0"
    assert payload["bid_volume_1"] == 1
    assert payload["ingest_type"] == "LIVE_CALLBACK"
    assert arguments[1] == 2
    assert "processed_market_tick:" not in " ".join(map(str, arguments))
    # 快照与订阅 Tick 的先后到达必须在 Lua 内原子比较，防止旧快照覆盖
    # 已落 Redis 的实时行情。
    assert "IGNORED_STALE" in arguments[0]
    assert "HGET" in arguments[0]


def test_publish_returns_ignored_stale_for_late_database_snapshot():
    redis_client = Mock()
    redis_client.eval.return_value = "IGNORED_STALE"

    result = MarketTickStore(redis_client).publish(normalize())

    assert result == MarketTickStoreResult.IGNORED_STALE


def test_option_latest_hash_uses_order_book_id_not_internal_symbol():
    redis_client = Mock()
    redis_client.eval.return_value = "PUBLISHED"
    tick = normalize().model_copy(
        update={
            "symbol": "jd2609-C-3200",
            "order_book_id": "JD2609C3200",
        }
    )

    MarketTickStore(redis_client).publish(tick)

    arguments = redis_client.eval.call_args.args
    assert arguments[2] == "market:latest:SHFE:JD2609C3200"


def test_hash_mapping_uses_empty_string_for_none_and_iso_time():
    mapping = MarketTickStore.tick_to_mapping(normalize())

    assert mapping["event_time"] == "2026-07-22T09:32:08+08:00"
    assert mapping["last_price"] == "14600.0"


def test_hash_mapping_restores_empty_optional_fields_to_none():
    original = normalize()
    mapping = MarketTickStore.tick_to_mapping(original)
    mapping["pre_close"] = ""
    mapping["server_time"] = ""
    mapping["raw_update_millisec"] = ""

    restored = MarketTickStore.mapping_to_tick(mapping)

    assert restored.source_event_id == original.source_event_id
    assert restored.last_price == original.last_price
    assert restored.pre_close is None
    assert restored.server_time is None
    assert restored.raw_update_millisec is None


def test_publish_writes_subscription_generation_atomically():
    redis_client = Mock()
    redis_client.eval.return_value = "PUBLISHED"
    store = MarketTickStore(redis_client)
    tick = normalize()

    result = store.publish(tick, subscription_generation=7)

    assert result == MarketTickStoreResult.PUBLISHED
    arguments = redis_client.eval.call_args.args
    assert arguments[1] == 2
    assert arguments[10] == "7"
    assert "'subscription_generation', ARGV[7]" in arguments[0]


def test_source_status_does_not_require_credentials():
    redis_client = Mock()
    store = MarketTickStore(redis_client)

    store.update_source_status({"status": "RUNNING", "last_error": ""})

    mapping = redis_client.hset.call_args.kwargs["mapping"]
    assert mapping == {"status": "RUNNING", "last_error": ""}
    redis_client.hdel.assert_called_once_with(
        "market:source:ymm_live_data:status",
        "duplicate_count",
        "stale_count",
        "age_stale_count",
        "no_tick_count",
    )
    assert not any("token" in key.lower() or "user" in key.lower() for key in mapping)


def test_source_status_removes_obsolete_fields_only_once():
    redis_client = Mock()
    store = MarketTickStore(redis_client)

    store.update_source_status({"status": "RUNNING"})
    store.update_source_status({"status": "RUNNING"})

    redis_client.hdel.assert_called_once()
    assert redis_client.hset.call_count == 2


def test_get_latest_many_uses_one_pipeline_and_stable_contract_mapping():
    redis_client = Mock()
    pipeline = redis_client.pipeline.return_value
    pipeline.execute.return_value = [
        {"last_price": "100"},
        {"last_price": "200"},
    ]
    store = MarketTickStore(redis_client)

    result = store.get_latest_many(
        [
            ("shfe", "rb2610"),
            ("DCE", "JD2609"),
            ("SHFE", "RB2610"),
        ]
    )

    assert result == {
        ("DCE", "JD2609"): {"last_price": "100"},
        ("SHFE", "RB2610"): {"last_price": "200"},
    }
    redis_client.pipeline.assert_called_once_with(transaction=False)
    assert pipeline.hgetall.call_count == 2


def test_get_latest_many_reads_option_with_canonical_order_book_key():
    redis_client = Mock()
    pipeline = redis_client.pipeline.return_value
    pipeline.execute.return_value = [{"last_price": "1019"}]
    store = MarketTickStore(redis_client)

    result = store.get_latest_many([("DCE", "jd2609-C-3200")])

    assert result == {("DCE", "JD2609-C-3200"): {"last_price": "1019"}}
    pipeline.hgetall.assert_called_once_with(
        "market:latest:DCE:JD2609C3200"
    )
