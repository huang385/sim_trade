from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from app.common.exceptions import BusinessRuleError
from app.infrastructure.client_market_subscription_store import (
    ClientMarketSubscriptionStore,
)


NOW = datetime(2026, 8, 11, 1, 2, 3, tzinfo=timezone.utc)


def test_request_codes_normalizes_connection_and_codes_with_one_lease():
    redis_client = Mock()
    redis_client.eval.return_value = 2
    store = ClientMarketSubscriptionStore(
        redis_client,
        ttl_seconds=90,
        max_codes_per_connection=50,
        now_provider=lambda: NOW,
    )

    expires_at = store.request_codes(
        connection_id="CONN 1",
        codes={"jd2609", "RB2610"},
    )

    assert expires_at == datetime.fromtimestamp(
        NOW.timestamp() + 90,
        tz=timezone.utc,
    )
    arguments = redis_client.eval.call_args.args
    assert arguments[4] == "CONN%201|"
    assert arguments[5] == 50
    assert set(arguments[7:]) == {
        "CONN%201|JD2609",
        "CONN%201|RB2610",
    }


def test_request_codes_rejects_connection_limit_atomically():
    redis_client = Mock()
    redis_client.eval.return_value = -1
    store = ClientMarketSubscriptionStore(
        redis_client,
        now_provider=lambda: NOW,
    )

    with pytest.raises(BusinessRuleError) as caught:
        store.request_codes(connection_id="C1", codes={"A", "B"})

    assert caught.value.error_code == "MARKET_SUBSCRIPTION_LIMIT_EXCEEDED"


def test_active_codes_support_bytes_and_deduplicate_connections():
    redis_client = Mock()
    redis_client.eval.return_value = [
        b"C1|JD2609",
        b"C2|JD2609",
        b"C2|IO2609-C-4000",
    ]
    store = ClientMarketSubscriptionStore(
        redis_client,
        now_provider=lambda: NOW,
    )

    assert store.list_active_contract_codes() == {
        "JD2609",
        "IO2609-C-4000",
    }


def test_remove_codes_and_connection_only_target_owner_members():
    redis_client = Mock()
    redis_client.zrem.return_value = 2
    redis_client.eval.return_value = 3
    store = ClientMarketSubscriptionStore(redis_client)

    assert store.remove_codes(connection_id="C 1", codes={"a", "B"}) == 2
    assert set(redis_client.zrem.call_args.args[1:]) == {
        "C%201|A",
        "C%201|B",
    }
    assert store.remove_connection("C 1") == 3
    assert redis_client.eval.call_args.args[3] == "C%201|"
