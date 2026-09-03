from unittest.mock import Mock

import pytest

from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.services.live_market_snapshot_service import (
    LiveMarketSnapshotService,
)
from tests.unit.services.test_market_tick_normalizer import normalize


def make_service(
    *,
    status_value: str = "RUNNING",
    subscribed_codes: str = "AG2609,RB2610",
    status_generation: str = "7",
    tick_generation: str = "7",
    ingest_type: str = "LIVE_CALLBACK",
    source: str = "YMM_LIVE_DATA",
    last_error: str = "",
):
    redis_client = Mock()
    pipeline = redis_client.pipeline.return_value
    tick = normalize()
    latest = MarketTickStore.tick_to_mapping(tick)
    latest.update(
        {
            "subscription_generation": tick_generation,
            "stream_message_id": "123-0",
            "ingest_type": ingest_type,
            "source": source,
        }
    )
    pipeline.execute.return_value = [
        {
            "status": status_value,
            "subscribed_codes": subscribed_codes,
            "subscription_generation": status_generation,
            "last_error": last_error,
        },
        latest,
    ]
    return LiveMarketSnapshotService(redis_client), tick


def test_current_live_callback_is_returned_as_matching_event():
    service, tick = make_service()

    event = service.get_matching_event(
        exchange_id=tick.exchange_id,
        order_book_id=tick.order_book_id,
        symbol=tick.symbol,
    )

    assert event is not None
    assert event.stream_message_id == "123-0"
    assert event.parsed_event.event_id == tick.source_event_id
    assert event.parsed_event.tick == tick


def test_uses_order_book_id_for_snapshot_key_when_symbol_is_different():
    service, tick = make_service(subscribed_codes="603680.XSHG")
    latest = service.redis_client.pipeline.return_value.execute.return_value[1]
    latest["order_book_id"] = "603680.XSHG"
    latest["symbol"] = "603680"

    event = service.get_matching_event(
        exchange_id=tick.exchange_id,
        order_book_id="603680.XSHG",
        symbol="603680",
    )

    assert event is not None
    hgetall_calls = service.redis_client.pipeline.return_value.hgetall.call_args_list
    assert "603680.XSHG" in hgetall_calls[1].args[0]


def test_internal_option_symbol_is_matched_case_insensitively():
    service, tick = make_service(subscribed_codes="JD2610P3500")
    latest = service.redis_client.pipeline.return_value.execute.return_value[1]
    latest["order_book_id"] = "JD2610P3500"
    latest["symbol"] = "jd2610-P-3500"

    event = service.get_matching_event(
        exchange_id=tick.exchange_id,
        order_book_id="JD2610P3500",
        symbol="jd2610-P-3500",
    )

    assert event is not None
    assert event.parsed_event.tick.symbol == "jd2610-P-3500"


@pytest.mark.parametrize(
    "overrides",
    [
        {"status_value": "DISCONNECTED"},
        {"subscribed_codes": "RB2610"},
        {"ingest_type": "REST_SNAPSHOT"},
    ],
)
def test_unready_or_non_live_snapshot_is_not_used(overrides):
    service, tick = make_service(**overrides)

    assert service.get_matching_event(
        exchange_id=tick.exchange_id,
        order_book_id=tick.order_book_id,
        symbol=tick.symbol,
    ) is None


def test_old_generation_tick_is_used_when_contract_is_subscribed():
    # 只要合约仍在当前订阅列表且行情源 RUNNING，旧订阅代次的行情也视为
    # 该合约最新有效盘口，可直接用于委托定价和到达撮合。
    service, tick = make_service(
        status_generation="8",
        tick_generation="7",
    )

    event = service.get_matching_event(
        exchange_id=tick.exchange_id,
        order_book_id=tick.order_book_id,
        symbol=tick.symbol,
    )

    assert event is not None
    assert event.stream_message_id == "123-0"


def test_partial_subscription_failure_does_not_block_successful_contract():
    service, tick = make_service(
        status_value="DEGRADED",
        last_error="SUBSCRIPTION_PARTIAL_FAILURE",
    )

    event = service.get_matching_event(
        exchange_id=tick.exchange_id,
        order_book_id=tick.order_book_id,
        symbol=tick.symbol,
    )

    assert event is not None


@pytest.mark.parametrize("last_error", ["", "QUEUE_FULL", "SOURCE_SLOW_CONSUMER"])
def test_source_level_degraded_state_still_fails_closed(last_error):
    service, tick = make_service(
        status_value="DEGRADED",
        last_error=last_error,
    )

    assert service.get_matching_event(
        exchange_id=tick.exchange_id,
        order_book_id=tick.order_book_id,
        symbol=tick.symbol,
    ) is None


def test_current_database_bootstrap_is_available_for_order_arrival():
    service, tick = make_service(
        ingest_type="REST_SNAPSHOT",
        source="YMM_DATA_SDK",
    )

    event = service.get_matching_event(
        exchange_id=tick.exchange_id,
        order_book_id=tick.order_book_id,
        symbol=tick.symbol,
    )

    assert event is not None
    assert event.parsed_event.tick.source == "YMM_DATA_SDK"


def test_explicit_bootstrap_snapshot_can_price_before_subscription_is_ready():
    service, tick = make_service(
        status_value="DISCONNECTED",
        subscribed_codes="",
        ingest_type="REST_SNAPSHOT",
        source="YMM_DATA_SDK",
    )

    event = service.get_matching_event(
        exchange_id=tick.exchange_id,
        order_book_id=tick.order_book_id,
        symbol=tick.symbol,
        allow_bootstrap_snapshot=True,
    )

    assert event is not None
    assert event.parsed_event.tick.source == "YMM_DATA_SDK"


def test_explicit_bootstrap_snapshot_must_match_expected_stream_message_id():
    service, tick = make_service(
        status_value="DISCONNECTED",
        subscribed_codes="",
        ingest_type="REST_SNAPSHOT",
        source="YMM_DATA_SDK",
    )

    assert service.get_matching_event(
        exchange_id=tick.exchange_id,
        order_book_id=tick.order_book_id,
        symbol=tick.symbol,
        allow_bootstrap_snapshot=True,
        expected_bootstrap_stream_message_id="older-snapshot-0",
    ) is None


def test_missing_stream_message_id_is_not_used():
    service, tick = make_service()
    pipeline = service.redis_client.pipeline.return_value
    pipeline.execute.return_value[1]["stream_message_id"] = ""

    assert service.get_matching_event(
        exchange_id=tick.exchange_id,
        order_book_id=tick.order_book_id,
        symbol=tick.symbol,
    ) is None
