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
from app.infrastructure.redis_keys import market_latest_key
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
        instrument=make_instrument(exchange_id=exchange_id, symbol=symbol),
    )


def test_real_redis_atomically_updates_latest_hash_and_appends_every_live_tick():
    suffix = uuid4().hex[:10].upper()
    exchange_id = f"ITM{suffix}"
    symbol = f"ITS{suffix}"
    stream_name = f"stream:test-market-ticks:{suffix}"
    store = MarketTickStore(redis_client, stream_name=stream_name)
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
            event_time=datetime(2026, 7, 22, 9, 32, 8),
            sequence_id=834,
        ),
        make_tick(
            exchange_id=exchange_id,
            symbol=symbol,
            event_time=datetime(2026, 7, 22, 9, 32, 8, 500_000),
            sequence_id=835,
        ),
        make_tick(
            exchange_id=exchange_id,
            symbol=symbol,
            event_time=datetime(2026, 7, 22, 9, 32, 9),
            sequence_id=1,
        ),
    ]
    latest_key = market_latest_key(exchange_id, symbol)
    cleanup_keys = [latest_key, stream_name]

    try:
        redis_client.ping()
        redis_client.delete(*cleanup_keys)
        for index, tick in enumerate(ticks):
            assert store.publish(
                tick,
                subscription_generation=(7 if index == len(ticks) - 1 else None),
            ) == MarketTickStoreResult.PUBLISHED

        latest = store.get_latest(exchange_id, symbol)
        assert latest["sequence_id"] == "1"
        assert latest["event_time"] == "2026-07-22T09:32:09+08:00"
        assert latest["bid_volume_1"] == "1"
        assert latest["ask_volume_1"] == "2"

        messages = redis_client.xrange(stream_name)
        assert len(messages) == 4
        # 最新Hash保留真实Stream编号，供订单到达即时撮合继续复用同一
        # 行情事件和成交幂等键。
        assert latest["stream_message_id"] == messages[-1][0]
        latest = store.get_latest(exchange_id, symbol)
        assert latest["subscription_generation"] == "7"
        # Hash增加的内部追踪字段不会影响MarketTick类型恢复。
        assert MarketTickStore.mapping_to_tick(latest) == ticks[-1]
        first_payload = json.loads(messages[0][1]["payload"])
        assert first_payload["last_price"] == "14600.0"
        assert first_payload["cumulative_turnover"] == "5298353100.0"

        # 新实现不会为每条行情创建独立幂等 Key。
        for tick in ticks:
            assert redis_client.exists(
                f"processed_market_tick:{tick.source_event_id}"
            ) == 0
    except RedisError as exc:
        pytest.skip(f"Redis不可用: {exc}")
    finally:
        try:
            redis_client.delete(*cleanup_keys)
        except RedisError:
            pass
