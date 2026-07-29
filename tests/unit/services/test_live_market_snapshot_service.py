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
        }
    )
    pipeline.execute.return_value = [
        {
            "status": status_value,
            "subscribed_codes": subscribed_codes,
            "subscription_generation": status_generation,
        },
        latest,
    ]
    return LiveMarketSnapshotService(redis_client), tick


def test_current_live_callback_is_returned_as_matching_event():
    service, tick = make_service()

    event = service.get_matching_event(
        exchange_id=tick.exchange_id,
        symbol=tick.symbol,
    )

    assert event is not None
    assert event.stream_message_id == "123-0"
    assert event.parsed_event.event_id == tick.source_event_id
    assert event.parsed_event.tick == tick


@pytest.mark.parametrize(
    "overrides",
    [
        {"status_value": "DISCONNECTED"},
        {"subscribed_codes": "RB2610"},
        {"status_generation": "8"},
        {"tick_generation": ""},
        {"ingest_type": "REST_SNAPSHOT"},
    ],
)
def test_unready_or_non_live_snapshot_is_not_used(overrides):
    service, tick = make_service(**overrides)

    assert service.get_matching_event(
        exchange_id=tick.exchange_id,
        symbol=tick.symbol,
    ) is None


def test_missing_stream_message_id_is_not_used():
    service, tick = make_service()
    pipeline = service.redis_client.pipeline.return_value
    pipeline.execute.return_value[1]["stream_message_id"] = ""

    assert service.get_matching_event(
        exchange_id=tick.exchange_id,
        symbol=tick.symbol,
    ) is None
