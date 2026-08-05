from datetime import datetime, timezone
import json

import pytest

from app.services.trade_created_pnl_service import (
    TradeCreatedPnlService,
)


class FakeCache:
    def __init__(self):
        self.invalidated = False

    def invalidate(self, **_kwargs):
        self.invalidated = True


class FakeStore:
    def __init__(self):
        self.contract_dirty = []
        self.account_dirty = []
        self.snapshot_writes = 0

    def mark_contract_dirty_once(self, **kwargs):
        self.contract_dirty.append(kwargs)
        return "7"

    def mark_account_fact_dirty_once(self, **kwargs):
        self.account_dirty.append(kwargs)
        return "3"

    def write_snapshots(self, **_kwargs):
        self.snapshot_writes += 1
        raise AssertionError("成交Worker不得直接写实时PnL快照")


def test_trade_only_marks_cross_process_dirty_contract():
    now = datetime(2026, 7, 27, 10, tzinfo=timezone.utc)
    store = FakeStore()
    cache = FakeCache()
    service = TradeCreatedPnlService(
        cache=cache,
        pnl_store=store,
    )
    fields = {
        "event_type": "TRADE_CREATED",
        "payload": json.dumps(
            {
                "event_id": "E001",
                "account_id": "A001",
                "exchange_id": "shfe",
                "symbol": "rb2610",
                "trade_time": now.isoformat(),
            }
        ),
    }

    result = service.process(
        stream_message_id="1-0",
        fields=fields,
    )

    assert result.action == "DIRTY_MARKED"
    assert result.dirty_version == "7"
    assert result.dirty_kind == "CONTRACT_STRUCTURE"
    assert cache.invalidated is True
    assert store.contract_dirty == [
        {
            "event_id": "E001",
            "exchange_id": "SHFE",
            "symbol": "RB2610",
            "account_id": "A001",
            "processed_ttl_seconds": 604800,
        }
    ]
    assert store.snapshot_writes == 0


def test_non_fact_event_is_skipped_without_dirty_or_snapshot_write():
    store = FakeStore()
    result = TradeCreatedPnlService(
        cache=FakeCache(),
        pnl_store=store,
    ).process(
        stream_message_id="1-0",
        fields={"event_type": "ORDER_FILLED", "payload": "{}"},
    )

    assert result.action == "SKIPPED"
    assert store.contract_dirty == []
    assert store.account_dirty == []
    assert store.snapshot_writes == 0


@pytest.mark.parametrize(
    "event_type",
    [
        "ORDER_ACCEPTED",
        "ORDER_CANCELLED",
        "ORDER_PARTIALLY_CANCELLED",
        "ORDER_MARGIN_UPDATED",
    ],
)
def test_order_events_mark_only_account_facts_dirty(event_type):
    store = FakeStore()
    result = TradeCreatedPnlService(
        cache=FakeCache(),
        pnl_store=store,
    ).process(
        stream_message_id="2-0",
        fields={
            "event_id": "E002",
            "event_type": event_type,
            "payload": json.dumps(
                {
                    "event_id": "E002",
                    "account_id": "A001",
                    "exchange_id": "DCE",
                    "symbol": "JD2609",
                }
            ),
        },
    )

    assert result.action == "DIRTY_MARKED"
    assert result.dirty_kind == "ACCOUNT_FACT"
    assert store.contract_dirty == []
    assert store.account_dirty == [
        {
            "event_id": "E002",
            "account_id": "A001",
            "processed_ttl_seconds": 604800,
        }
    ]


def test_account_fact_event_does_not_duplicate_source_business_dirty():
    store = FakeStore()
    result = TradeCreatedPnlService(
        cache=FakeCache(),
        pnl_store=store,
    ).process(
        stream_message_id="3-0",
        fields={
            "event_id": "E003",
            "event_type": "ACCOUNT_FACT_UPDATED",
            "payload": json.dumps(
                {"event_id": "E003", "account_id": "A001"}
            ),
        },
    )

    assert result.action == "SKIPPED"
    assert store.account_dirty == []
    assert store.contract_dirty == []


def test_position_fact_event_refreshes_only_affected_contract():
    store = FakeStore()
    result = TradeCreatedPnlService(
        cache=FakeCache(),
        pnl_store=store,
    ).process(
        stream_message_id="4-0",
        fields={
            "event_id": "E004",
            "event_type": "POSITION_UPDATED",
            "payload": json.dumps(
                {
                    "event_id": "E004",
                    "account_id": "A001",
                    "exchange_id": "DCE",
                    "symbol": "JD2609-C-4000",
                    "fact_reason": "OPTION_MARGIN_ADJUSTMENT",
                }
            ),
        },
    )

    assert result.dirty_kind == "CONTRACT_STRUCTURE"
    assert store.contract_dirty[0]["event_id"] == "E004"
