import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from app.enums.order_enums import (
    OffsetFlag,
    OrderDirection,
    OrderStatus,
    OrderType,
)
from app.matching.base import MatchingEngine
from app.matching.types import (
    MatchResult,
    MatchingMarketData,
    MatchingOrder,
    MatchingOrderCandidate,
)
from app.services.market_tick_matching_service import (
    MarketTickMatchingService,
    UnsupportedMarketTickEventError,
)
from app.services.trade_settlement_service import (
    SettlementCommand,
    SettlementResult,
)


def make_fields(*, ingest_type="LIVE_CALLBACK", source="YMM_LIVE_DATA"):
    payload = {
        "source_event_id": "TICK-1",
        "source": source,
        "ingest_type": ingest_type,
        "order_book_id": "AG2609",
        "exchange_id": "SHFE",
        "symbol": "AG2609",
        "instrument_type": "FUTURES",
        "trading_day": date(2026, 7, 23).isoformat(),
        "event_time": datetime(
            2026, 7, 23, 1, tzinfo=timezone.utc
        ).isoformat(),
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


def make_order(order_id, **overrides):
    values = {
        "order_id": order_id,
        "status": "ACCEPTED",
        "remaining_volume": 5,
        "order_type": "LIMIT",
        "offset_flag": "OPEN",
        "exchange_id": "SHFE",
        "symbol": "AG2609",
        "instrument_type": "FUTURES",
        "direction": "BUY",
        "limit_price": Decimal("14600"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeMatchingEngine:
    """可注入的结构化 Fake，证明 Service 不依赖 VN 具体类型。"""

    name = "FAKE"
    version = "test"

    def __init__(self, matched=True):
        self.matched = matched
        self.calls: list[tuple[MatchingOrder, MatchingMarketData]] = []
        self.results: list[MatchResult] = []

    def match(
        self,
        order: MatchingOrder,
        market: MatchingMarketData,
    ) -> MatchResult:
        self.calls.append((order, market))
        result = MatchResult(
            matched=self.matched,
            fill_price=market.ask_price_1 if self.matched else None,
            fill_volume=min(
                order.remaining_volume,
                market.ask_volume_1,
            )
            if self.matched
            else 0,
            reason=None if self.matched else "FAKE_NOT_MATCHED",
            engine_name=self.name,
            engine_version=self.version,
        )
        self.results.append(result)
        return result


def make_service(
    *,
    orders=None,
    matched=True,
    settlement_side_effect=None,
):
    orders = orders or [make_order("O-1"), make_order("O-2")]
    session_factory = Mock()

    def context():
        manager = MagicMock()
        manager.__enter__.return_value = Mock()
        return manager

    session_factory.side_effect = context
    active_index = Mock()
    active_index.list_instrument_order_ids.return_value = {
        order.order_id for order in orders
    }
    repository = Mock()
    repository.get_by_order_id.side_effect = orders
    settlement = Mock()
    if settlement_side_effect is None:
        settlement.settle.side_effect = [
            SettlementResult(f"T-{index}", order.order_id, "SETTLED")
            for index, order in enumerate(orders, start=1)
        ]
    else:
        settlement.settle.side_effect = settlement_side_effect
    engine = FakeMatchingEngine(matched=matched)
    service = MarketTickMatchingService(
        session_factory=session_factory,
        active_order_index=active_index,
        order_repository=repository,
        matching_engine=engine,
        settlement_service=settlement,
    )
    return service, engine, settlement


def test_fake_engine_conforms_to_matching_engine_interface():
    assert isinstance(FakeMatchingEngine(), MatchingEngine)


def test_rest_snapshot_never_triggers_matching():
    service, engine, settlement = make_service()

    with pytest.raises(UnsupportedMarketTickEventError):
        service.process(
            stream_message_id="1-0",
            fields=make_fields(ingest_type="REST_SNAPSHOT"),
        )

    assert engine.calls == []
    settlement.settle.assert_not_called()


def test_database_bootstrap_snapshot_triggers_existing_matching_chain():
    service, engine, settlement = make_service()

    result = service.process(
        stream_message_id="1-0",
        fields=make_fields(
            ingest_type="REST_SNAPSHOT",
            source="YMM_DATA_SDK",
        ),
    )

    assert result.settled_count == 2
    assert len(engine.calls) == 2
    assert settlement.settle.call_count == 2


def test_limit_open_orders_each_call_injected_engine_and_settle():
    service, engine, settlement = make_service()

    result = service.process(stream_message_id="1-0", fields=make_fields())

    assert result.candidate_count == 2
    assert result.matched_count == 2
    assert result.settled_count == 2
    assert result.skipped_count == 0
    assert len(engine.calls) == 2
    assert settlement.settle.call_count == 2
    assert all(
        isinstance(order, MatchingOrder)
        and isinstance(market, MatchingMarketData)
        for order, market in engine.calls
    )
    assert all(not hasattr(order, "order_id") for order, _ in engine.calls)
    assert all(
        not hasattr(market, "stream_message_id")
        and not hasattr(market, "event_id")
        for _, market in engine.calls
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"offset_flag": "UNKNOWN"},
        {"order_type": "MARKET"},
    ],
)
def test_unsupported_order_is_filtered_before_engine(overrides):
    service, engine, settlement = make_service(
        orders=[make_order("O-1", **overrides)]
    )

    result = service.process(stream_message_id="1-0", fields=make_fields())

    assert result.candidate_count == 1
    assert result.matched_count == 0
    assert result.skipped_count == 1
    assert engine.calls == []
    settlement.settle.assert_not_called()


def test_not_matched_result_does_not_call_settlement():
    service, engine, settlement = make_service(
        orders=[make_order("O-1")],
        matched=False,
    )

    result = service.process(stream_message_id="1-0", fields=make_fields())

    assert len(engine.calls) == 1
    assert result.matched_count == 0
    assert result.settled_count == 0
    assert result.skipped_count == 1
    settlement.settle.assert_not_called()


def test_matched_result_is_wrapped_in_complete_settlement_command():
    service, engine, settlement = make_service(orders=[make_order("O-1")])

    result = service.process(stream_message_id="1-0", fields=make_fields())

    assert len(engine.calls) == 1
    settlement.settle.assert_called_once()
    command = settlement.settle.call_args.args[1]
    assert isinstance(command, SettlementCommand)
    assert command.order_id == "O-1"
    assert command.market_event_id == "TICK-1"
    assert command.market_stream_message_id == "1-0"
    assert command.tick_event_time == datetime(
        2026, 7, 23, 1, tzinfo=timezone.utc
    )
    assert command.tick_sequence_id == 1
    assert command.match_result is engine.results[0]
    assert command.match_result.engine_name == "FAKE"
    assert result.settled_count == 1


def test_one_order_failure_does_not_stop_later_order_but_tick_retries():
    service, engine, settlement = make_service(
        settlement_side_effect=[
            RuntimeError("temporary database error"),
            SettlementResult("T-2", "O-2", "SETTLED"),
        ]
    )

    with pytest.raises(RuntimeError, match="temporary database error"):
        service.process(stream_message_id="1-0", fields=make_fields())

    # 第二笔仍先完成；整条 Tick 不 ACK，重试时第一笔继续处理，第二笔走幂等。
    assert len(engine.calls) == 2
    assert settlement.settle.call_count == 2


def test_order_arrival_only_queries_and_matches_the_new_candidate():
    new_order = make_order("O-NEW")
    service, engine, settlement = make_service(orders=[new_order])
    service.active_order_index.list_instrument_order_ids.return_value = {
        f"O-{index}" for index in range(100)
    }
    event = service.parse_event(make_fields())

    result = service.process_candidate_order(
        order_id="O-NEW",
        event=event,
        stream_message_id="1-0",
    )

    assert result.candidate_count == 1
    service.active_order_index.list_instrument_order_ids.assert_not_called()
    service.order_repository.get_by_order_id.assert_called_once()
    assert len(engine.calls) == 1
    settlement.settle.assert_called_once()


def test_order_arrival_reuses_immutable_snapshot_without_ordinary_query():
    service, engine, settlement = make_service(orders=[])
    event = service.parse_event(make_fields())
    snapshot = MatchingOrderCandidate(
        order_id="O-SNAPSHOT",
        exchange_id="SHFE",
        symbol="AG2609",
        status=OrderStatus.ACCEPTED,
        order=MatchingOrder(
            direction=OrderDirection.BUY,
            offset_flag=OffsetFlag.OPEN,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("14600"),
            remaining_volume=5,
        ),
    )

    result = service.process_candidate_order(
        order_id="O-SNAPSHOT",
        event=event,
        stream_message_id="1-0",
        order_snapshot=snapshot,
    )

    assert result.candidate_count == 1
    service.order_repository.get_by_order_id.assert_not_called()
    assert len(engine.calls) == 1
    settlement.settle.assert_called_once()
    assert settlement.settle.call_args.args[1].order_id == "O-SNAPSHOT"
