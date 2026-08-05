from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from redis.exceptions import RedisError

from app.common.exceptions import BusinessRuleError
from app.core.redis_client import redis_client
from app.infrastructure.market_pre_subscription_store import (
    MarketPreSubscriptionStore,
)


pytestmark = pytest.mark.integration


def test_real_redis_pre_subscription_expires_and_enforces_account_limit():
    key = f"market:test-pre-subscriptions:{uuid4().hex}"
    current = [datetime(2026, 8, 4, 1, tzinfo=timezone.utc)]
    try:
        redis_client.ping()
    except RedisError as exc:
        pytest.skip(f"Redis不可用: {exc}")

    store = MarketPreSubscriptionStore(
        redis_client,
        key=key,
        ttl_seconds=60,
        max_codes_per_account=2,
        now_provider=lambda: current[0],
    )
    try:
        store.request_codes(
            account_id="A001",
            codes={"JD2609-C-4000", "JD2609"},
        )
        store.request_codes(
            account_id="A002",
            codes={"JD2609"},
        )

        assert store.list_active_contract_codes() == {
            "JD2609-C-4000",
            "JD2609",
        }
        assert set(store.get_account_requests("A001")) == {
            "JD2609-C-4000",
            "JD2609",
        }
        with pytest.raises(BusinessRuleError) as caught:
            store.request_codes(account_id="A001", codes={"EXTRA"})
        assert caught.value.error_code == (
            "MARKET_PRE_SUBSCRIPTION_LIMIT_EXCEEDED"
        )

        current[0] += timedelta(seconds=61)
        assert store.list_active_contract_codes() == set()
        assert redis_client.zcard(key) == 0
    finally:
        redis_client.delete(key)
