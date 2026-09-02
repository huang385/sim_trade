"""两个行情域在真实 Redis 上的隔离与持续写入压力测试。"""

from uuid import uuid4

from app.core.redis_client import redis_client
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.redis_keys import market_latest_key
from app.schemas.market_tick_schema import MarketTickIngestType
from tests.unit.services.test_market_tick_normalizer import normalize


def test_two_market_domains_publish_2000_ticks_without_cross_stream_writes():
    suffix = uuid4().hex.upper()
    futures_stream = f"test:market:futures:{suffix}"
    securities_stream = f"test:market:securities:{suffix}"
    futures_status = f"test:market:futures:status:{suffix}"
    securities_status = f"test:market:securities:status:{suffix}"
    futures_code = f"FU{suffix[:10]}"
    securities_code = f"ST{suffix[:10]}.XSHG"
    futures_store = MarketTickStore(
        redis_client,
        stream_name=futures_stream,
        source_status_key=futures_status,
    )
    securities_store = MarketTickStore(
        redis_client,
        stream_name=securities_stream,
        source_status_key=securities_status,
    )
    cleanup_keys = (
        futures_stream,
        securities_stream,
        futures_status,
        securities_status,
        market_latest_key("ITF", futures_code),
        market_latest_key("ITS", securities_code),
    )
    try:
        for sequence in range(1000):
            futures_store.publish(
                normalize().model_copy(
                    update={
                        "source_event_id": f"F-{suffix}-{sequence}",
                        "exchange_id": "ITF",
                        "symbol": futures_code,
                        "order_book_id": futures_code,
                        "ingest_type": MarketTickIngestType.LIVE_CALLBACK,
                    }
                )
            )
            securities_store.publish(
                normalize().model_copy(
                    update={
                        "source_event_id": f"S-{suffix}-{sequence}",
                        "exchange_id": "ITS",
                        "symbol": securities_code,
                        "order_book_id": securities_code,
                        "ingest_type": MarketTickIngestType.LIVE_CALLBACK,
                    }
                )
            )

        assert redis_client.xlen(futures_stream) == 1000
        assert redis_client.xlen(securities_stream) == 1000
        assert futures_store.get_latest("ITF", futures_code)[
            "source_event_id"
        ] == f"F-{suffix}-999"
        assert securities_store.get_latest("ITS", securities_code)[
            "source_event_id"
        ] == f"S-{suffix}-999"
        futures_store.update_source_status({"status": "RUNNING"})
        securities_store.update_source_status({"status": "DEGRADED"})
        assert redis_client.hget(futures_status, "status") == "RUNNING"
        assert redis_client.hget(securities_status, "status") == "DEGRADED"
    finally:
        redis_client.delete(*cleanup_keys)
