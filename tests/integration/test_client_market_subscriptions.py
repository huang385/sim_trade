from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.core.redis_client import redis_client
from app.infrastructure.client_market_subscription_store import (
    ClientMarketSubscriptionStore,
)


def test_real_redis_client_subscription_lease_deduplicates_and_expires():
    key = f"test:market:client-subscriptions:{uuid4().hex}"
    now = [datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)]
    store = ClientMarketSubscriptionStore(
        redis_client,
        key=key,
        ttl_seconds=30,
        max_codes_per_connection=3,
        now_provider=lambda: now[0],
    )
    try:
        store.request_codes(connection_id="C1", codes={"jd2609", "RB2610"})
        store.request_codes(connection_id="C2", codes={"JD2609"})
        assert store.list_active_contract_codes() == {"JD2609", "RB2610"}

        store.remove_codes(connection_id="C1", codes={"JD2609"})
        # C2仍持有同一合约，聚合需求不能被C1退订误删。
        assert store.list_active_contract_codes() == {"JD2609", "RB2610"}

        store.remove_connection("C2")
        assert store.list_active_contract_codes() == {"RB2610"}

        now[0] += timedelta(seconds=31)
        assert store.list_active_contract_codes() == set()
        assert redis_client.zcard(key) == 0
    finally:
        redis_client.delete(key)
