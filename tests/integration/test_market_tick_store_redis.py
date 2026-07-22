import json
from datetime import datetime
from uuid import uuid4

import pytest
from redis.exceptions import RedisError

from app.core.redis_client import redis_client
from app.infrastructure.market_data.market_tick_store import (
    MarketTickStore,
    MarketTickStoreResult,
)
from app.infrastructure.redis_keys import (
    market_latest_key,
    processed_market_tick_key,
)
from app.services.market_tick_normalizer import MarketTickNormalizer
from tests.unit.services.test_market_tick_normalizer import (
    make_data,
    make_instrument,
    make_raw,
)


pytestmark = pytest.mark.integration


def make_tick(*, exchange_id, symbol, event_time, sequence_id):
    data = make_data(
        exchange=exchange_id,
        event_time=event_time,
        sequence_id=sequence_id,
    )
    return MarketTickNormalizer().normalize(
        data=data,
        raw=make_raw(data),
        instrument=make_instrument(
            exchange_id=exchange_id,
            symbol=symbol,
        ),
    )


def test_real_redis_atomically_filters_duplicate_stale_and_sequence_reset():
    suffix = uuid4().hex[:10].upper()
    exchange_id = f"ITM{suffix}"
    symbol = f"ITS{suffix}"
    stream_name = f"stream:test-market-ticks:{suffix}"
    store = MarketTickStore(
        redis_client,
        stream_name=stream_name,
        processed_ttl_seconds=300,
    )
    ticks = [
        make_tick(
            exchange_id=exchange_id,
            symbol=symbol,
            event_time=datetime(2026, 7, 22, 9, 32, 8),
            sequence_id=833,
        ),
        make_tick(
            exchange_id=exchange_id,
            symbol=symbol,
            event_time=datetime(2026, 7, 22, 9, 32, 7),
            sequence_id=999,
        ),
        make_tick(
            exchange_id=exchange_id,
            symbol=symbol,
            event_time=datetime(2026, 7, 22, 9, 32, 8),
            sequence_id=834,
        ),
        make_tick(
            exchange_id=exchange_id,
            symbol=symbol,
            event_time=datetime(2026, 7, 22, 9, 32, 9),
            sequence_id=1,
        ),
    ]
    cleanup_keys = [
        market_latest_key(exchange_id, symbol),
        stream_name,
        *(processed_market_tick_key(tick.source_event_id) for tick in ticks),
    ]

    try:
        redis_client.ping()
        redis_client.delete(*cleanup_keys)
        assert store.publish(ticks[0]) == MarketTickStoreResult.PUBLISHED
        assert store.publish(ticks[0]) == MarketTickStoreResult.DUPLICATE
        assert store.publish(ticks[1]) == MarketTickStoreResult.STALE
        assert store.publish(ticks[2]) == MarketTickStoreResult.PUBLISHED
        # event_time更新时，即使sequence_id重置为1也属于新行情。
        assert store.publish(ticks[3]) == MarketTickStoreResult.PUBLISHED

        latest = store.get_latest(exchange_id, symbol)
        assert latest["sequence_id"] == "1"
        assert latest["event_time"] == "2026-07-22T09:32:09"
        assert latest["bid_volume_1"] == "1"
        assert latest["ask_volume_1"] == "2"
        messages = redis_client.xrange(stream_name)
        assert len(messages) == 3
        first_payload = json.loads(messages[0][1]["payload"])
        assert first_payload["last_price"] == "14600.0"
        assert first_payload["cumulative_turnover"] == "5298353100.0"
    except RedisError as exc:
        pytest.skip(f"Redis不可用: {exc}")
    finally:
        try:
            redis_client.delete(*cleanup_keys)
        except RedisError:
            pass
