from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType
from app.schemas.matching_schema import MatchableOrder
from app.services.vn_matching_engine import VNMatchingEngine


def make_tick(**overrides) -> MarketTick:
    values = {
        "source_event_id": "TICK-1",
        "ingest_type": MarketTickIngestType.LIVE_CALLBACK,
        "order_book_id": "AG2609",
        "exchange_id": "SHFE",
        "symbol": "AG2609",
        "trading_day": date(2026, 7, 23),
        "event_time": datetime(2026, 7, 23, 9, tzinfo=timezone.utc),
        "sequence_id": 1,
        "cumulative_volume": 100,
        "bid_price_1": Decimal("14598"),
        "bid_volume_1": 3,
        "ask_price_1": Decimal("14599"),
        "ask_volume_1": 2,
    }
    values.update(overrides)
    return MarketTick(**values)


def make_order(**overrides) -> MatchableOrder:
    values = {
        "order_id": "O-1",
        "direction": "BUY",
        "offset_flag": "OPEN",
        "order_type": "LIMIT",
        "limit_price": Decimal("14600"),
        "remaining_volume": 5,
    }
    values.update(overrides)
    return MatchableOrder(**values)


@pytest.mark.parametrize("limit_price", [Decimal("14599"), Decimal("14600")])
def test_buy_equal_or_higher_than_ask_matches(limit_price):
    result = VNMatchingEngine().match_limit_open_order(
        order=make_order(limit_price=limit_price),
        tick=make_tick(),
        market_stream_message_id="1-0",
    )
    assert result.matched is True
    assert result.fill_price == Decimal("14599")
    assert result.fill_volume == 2


def test_buy_below_ask_does_not_match():
    result = VNMatchingEngine().match_limit_open_order(
        order=make_order(limit_price=Decimal("14598")),
        tick=make_tick(),
        market_stream_message_id="1-0",
    )
    assert result.matched is False
    assert result.reason == "BUY_LIMIT_NOT_REACHED"


@pytest.mark.parametrize("limit_price", [Decimal("14597"), Decimal("14598")])
def test_sell_equal_or_lower_than_bid_matches(limit_price):
    result = VNMatchingEngine().match_limit_open_order(
        order=make_order(direction="SELL", limit_price=limit_price),
        tick=make_tick(),
        market_stream_message_id="1-0",
    )
    assert result.matched is True
    assert result.fill_price == Decimal("14598")
    assert result.fill_volume == 3


def test_sell_above_bid_does_not_match():
    result = VNMatchingEngine().match_limit_open_order(
        order=make_order(direction="SELL", limit_price=Decimal("14599")),
        tick=make_tick(),
        market_stream_message_id="1-0",
    )
    assert result.matched is False
    assert result.reason == "SELL_LIMIT_NOT_REACHED"


@pytest.mark.parametrize(
    ("order_overrides", "tick_overrides", "reason"),
    [
        ({}, {"ask_price_1": None}, "INVALID_ASK_PRICE"),
        ({}, {"ask_volume_1": 0}, "NO_ASK_VOLUME"),
        ({"direction": "SELL"}, {"bid_price_1": None}, "INVALID_BID_PRICE"),
        ({"direction": "SELL"}, {"bid_volume_1": 0}, "NO_BID_VOLUME"),
        ({"order_type": "MARKET"}, {}, "UNSUPPORTED_ORDER_TYPE"),
        ({"offset_flag": "CLOSE"}, {}, "UNSUPPORTED_OFFSET_FLAG"),
        ({"remaining_volume": 0}, {}, "NO_REMAINING_VOLUME"),
    ],
)
def test_invalid_or_unsupported_order_does_not_match(
    order_overrides, tick_overrides, reason
):
    result = VNMatchingEngine().match_limit_open_order(
        order=make_order(**order_overrides),
        tick=make_tick(**tick_overrides),
        market_stream_message_id="1-0",
    )
    assert result.matched is False
    assert result.fill_price is None
    assert result.fill_volume == 0
    assert result.reason == reason


def test_complete_fill_uses_remaining_volume_and_keeps_decimal():
    result = VNMatchingEngine().match_limit_open_order(
        order=make_order(remaining_volume=1),
        tick=make_tick(ask_price_1=Decimal("14599.123456"), ask_volume_1=10),
        market_stream_message_id="1-0",
    )
    assert result.fill_price == Decimal("14599.123456")
    assert isinstance(result.fill_price, Decimal)
    assert result.fill_volume == 1


def test_vn_orders_independently_receive_full_displayed_liquidity():
    engine = VNMatchingEngine()
    tick = make_tick(ask_volume_1=3)
    results = [
        engine.match_limit_open_order(
            order=make_order(order_id=f"O-{index}"),
            tick=tick,
            market_stream_message_id="1-0",
        )
        for index in range(2)
    ]
    # VN 模式不共享盘口量，两笔订单都可以各自成交3手。
    assert [item.fill_volume for item in results] == [3, 3]
