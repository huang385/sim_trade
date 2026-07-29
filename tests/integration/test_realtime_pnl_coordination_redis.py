from uuid import uuid4

import pytest

from app.core.redis_client import redis_client
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.infrastructure.redis_keys import (
    PNL_DIRTY_CONTRACTS_KEY,
    PNL_DIRTY_CONTRACT_VERSIONS_KEY,
    pnl_dirty_contract_accounts_key,
    pnl_dirty_contract_member,
    processed_pnl_fact_event_key,
)


pytestmark = pytest.mark.integration


def test_trade_dirty_version_cas_preserves_newer_trade():
    """真实Redis验证计算期间的新成交不会被旧周期清除。"""

    try:
        redis_client.ping()
    except Exception as exc:
        pytest.skip(f"Redis不可连接: {exc}")

    suffix = uuid4().hex[:10].upper()
    exchange_id = "ITEX"
    symbol = f"DIRTY{suffix}"
    member = pnl_dirty_contract_member(exchange_id, symbol)
    accounts_key = pnl_dirty_contract_accounts_key(
        exchange_id,
        symbol,
    )
    store = RealtimePnlStore(redis_client)
    try:
        first = store.mark_contract_dirty(
            exchange_id=exchange_id,
            symbol=symbol,
            account_id="A001",
        )
        second = store.mark_contract_dirty(
            exchange_id=exchange_id,
            symbol=symbol,
            account_id="A002",
        )

        assert first != second
        assert (
            store.complete_dirty_contract(
                exchange_id=exchange_id,
                symbol=symbol,
                expected_version=first,
            )
            is False
        )
        assert redis_client.smembers(accounts_key) == {"A001", "A002"}
        assert (
            store.complete_dirty_contract(
                exchange_id=exchange_id,
                symbol=symbol,
                expected_version=second,
            )
            is True
        )
        assert redis_client.sismember(PNL_DIRTY_CONTRACTS_KEY, member) == 0
        assert redis_client.exists(accounts_key) == 0
    finally:
        redis_client.srem(PNL_DIRTY_CONTRACTS_KEY, member)
        redis_client.hdel(PNL_DIRTY_CONTRACT_VERSIONS_KEY, member)
        redis_client.delete(accounts_key)


def test_account_fact_event_marks_dirty_only_once():
    """真实Redis验证Outbox重投不会重复递增缓存版本。"""

    try:
        redis_client.ping()
    except Exception as exc:
        pytest.skip(f"Redis不可连接: {exc}")

    suffix = uuid4().hex[:10].upper()
    event_id = f"FACT-{suffix}"
    exchange_id = "ITEX"
    symbol = f"FACT{suffix}"
    member = pnl_dirty_contract_member(exchange_id, symbol)
    accounts_key = pnl_dirty_contract_accounts_key(
        exchange_id,
        symbol,
    )
    processed_key = processed_pnl_fact_event_key(event_id)
    store = RealtimePnlStore(redis_client)
    try:
        first = store.mark_contract_dirty_once(
            event_id=event_id,
            exchange_id=exchange_id,
            symbol=symbol,
            account_id="A001",
            processed_ttl_seconds=60,
        )
        duplicate = store.mark_contract_dirty_once(
            event_id=event_id,
            exchange_id=exchange_id,
            symbol=symbol,
            account_id="A001",
            processed_ttl_seconds=60,
        )

        assert first is not None
        assert duplicate is None
        assert redis_client.smembers(accounts_key) == {"A001"}
    finally:
        redis_client.srem(PNL_DIRTY_CONTRACTS_KEY, member)
        redis_client.hdel(PNL_DIRTY_CONTRACT_VERSIONS_KEY, member)
        redis_client.delete(accounts_key, processed_key)
