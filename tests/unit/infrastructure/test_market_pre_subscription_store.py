from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from app.common.exceptions import BusinessRuleError
from app.infrastructure.market_pre_subscription_store import (
    MarketPreSubscriptionStore,
)


NOW = datetime(2026, 8, 4, 1, 2, 3, tzinfo=timezone.utc)


def test_request_codes_normalizes_codes_and_sets_one_expiry():
    redis_client = Mock()
    redis_client.eval.return_value = 2
    store = MarketPreSubscriptionStore(
        redis_client,
        ttl_seconds=60,
        max_codes_per_account=20,
        now_provider=lambda: NOW,
    )

    expires_at = store.request_codes(
        account_id="A 001",
        codes={"jd2609-c-4000", "JD2609"},
    )

    assert expires_at == datetime.fromtimestamp(
        NOW.timestamp() + 60,
        tz=timezone.utc,
    )
    arguments = redis_client.eval.call_args.args
    assert arguments[1] == 1
    assert arguments[4] == "A%20001|"
    assert arguments[5] == 20
    assert set(arguments[7:]) == {
        "A%20001|JD2609-C-4000",
        "A%20001|JD2609",
    }


def test_request_codes_rejects_account_limit_atomically():
    redis_client = Mock()
    redis_client.eval.return_value = -1
    store = MarketPreSubscriptionStore(
        redis_client,
        now_provider=lambda: NOW,
    )

    with pytest.raises(BusinessRuleError) as caught:
        store.request_codes(account_id="A001", codes={"A", "B"})

    assert caught.value.error_code == (
        "MARKET_PRE_SUBSCRIPTION_LIMIT_EXCEEDED"
    )


def test_active_rows_support_bytes_and_deduplicate_codes_across_accounts():
    redis_client = Mock()
    expiry = NOW.timestamp() + 60
    redis_client.eval.return_value = [
        b"A001|JD2609",
        str(expiry).encode(),
        b"A002|JD2609",
        str(expiry).encode(),
        b"A002|IO2609-C-4000",
        str(expiry).encode(),
    ]
    store = MarketPreSubscriptionStore(
        redis_client,
        now_provider=lambda: NOW,
    )

    assert store.list_active_contract_codes() == {
        "JD2609",
        "IO2609-C-4000",
    }
    assert set(store.get_account_requests("A002")) == {
        "JD2609",
        "IO2609-C-4000",
    }
