import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from app.schemas.matching_schema import SettlementResult
from app.services.market_tick_matching_service import (
    MarketTickMatchingService,
    UnsupportedMarketTickEventError,
)
from app.services.vn_matching_engine import VNMatchingEngine


def make_fields(*, ingest_type="LIVE_CALLBACK"):
    payload = {
        "source_event_id": "TICK-1",
        "source": "YML_FEEDHUB",
        "ingest_type": ingest_type,
        "order_book_id": "AG2609",
        "exchange_id": "SHFE",
        "symbol": "AG2609",
        "trading_day": date(2026, 7, 23).isoformat(),
        "event_time": datetime(2026, 7, 23, 1, tzinfo=timezone.utc).isoformat(),
        "sequence_id": 1,
        "cumulative_volume": 10,
        "bid_price_1": "14598",
        "bid_volume_1": 3,
        "ask_price_1": "14599",
        "ask_volume_1": 3,
    }
    return {
        "event_id": "TICK-1",
        "event_type": "MARKET_TICK",
        "exchange_id": "SHFE",
        "symbol": "AG2609",
        "payload": json.dumps(payload),
    }


def make_order(order_id):
    return SimpleNamespace(
        order_id=order_id,
        status="ACCEPTED",
        remaining_volume=5,
        order_type="LIMIT",
        offset_flag="OPEN",
        exchange_id="SHFE",
        symbol="AG2609",
        direction="BUY",
        limit_price=Decimal("14600"),
    )


def make_service(*, settlement_side_effect=None):
    session_factory = Mock()

    def context():
        manager = MagicMock()
        manager.__enter__.return_value = Mock()
        return manager

    session_factory.side_effect = context
    active_index = Mock()
    active_index.list_instrument_order_ids.return_value = {"O-1", "O-2"}
    repository = Mock()
    repository.get_by_order_id.side_effect = [make_order("O-1"), make_order("O-2")]
    settlement = Mock()
    if settlement_side_effect is None:
        settlement.settle.side_effect = [
            SettlementResult("T-1", "O-1", "SETTLED"),
            SettlementResult("T-2", "O-2", "SETTLED"),
        ]
    else:
        settlement.settle.side_effect = settlement_side_effect
    service = MarketTickMatchingService(
        session_factory=session_factory,
        active_order_index=active_index,
        order_repository=repository,
        matching_engine=VNMatchingEngine(),
        settlement_service=settlement,
    )
    return service, settlement


def test_rest_snapshot_never_triggers_matching():
    service, settlement = make_service()
    with pytest.raises(UnsupportedMarketTickEventError):
        service.process(stream_message_id="1-0", fields=make_fields(ingest_type="REST_SNAPSHOT"))
    settlement.settle.assert_not_called()


def test_one_tick_settles_multiple_orders_with_independent_liquidity():
    service, settlement = make_service()
    result = service.process(stream_message_id="1-0", fields=make_fields())
    assert result.candidate_count == 2
    assert result.matched_count == 2
    assert result.settled_count == 2
    assert settlement.settle.call_count == 2
    assert [call.args[1].fill_volume for call in settlement.settle.call_args_list] == [3, 3]


def test_one_order_failure_does_not_stop_later_order_but_tick_retries():
    service, settlement = make_service(
        settlement_side_effect=[
            RuntimeError("temporary database error"),
            SettlementResult("T-2", "O-2", "SETTLED"),
        ]
    )
    with pytest.raises(RuntimeError, match="temporary database error"):
        service.process(stream_message_id="1-0", fields=make_fields())
    # 第二笔仍先完成；整条 Tick 不 ACK，重试时第一笔继续处理，第二笔走幂等。
    assert settlement.settle.call_count == 2
