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
    assert "HGET" not in arguments[0]


def test_hash_mapping_uses_empty_string_for_none_and_iso_time():
    mapping = MarketTickStore.tick_to_mapping(normalize())

    assert mapping["event_time"] == "2026-07-22T09:32:08+08:00"
    assert mapping["last_price"] == "14600.0"


def test_source_status_does_not_require_credentials():
    redis_client = Mock()
    store = MarketTickStore(redis_client)

    store.update_source_status({"status": "RUNNING", "last_error": ""})

    mapping = redis_client.hset.call_args.kwargs["mapping"]
    assert mapping == {"status": "RUNNING", "last_error": ""}
    redis_client.hdel.assert_called_once_with(
        "market:source:yml_feedhub:status",
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
