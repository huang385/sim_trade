from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.enums.order_enums import OffsetFlag, OrderDirection, OrderType
from app.matching.engines.vn import VnMatchingEngine
from app.matching.models import MatchingMarketData, MatchingOrder


def make_market(**overrides) -> MatchingMarketData:
    """创建不依赖 Pydantic 或数据库的一档行情快照。"""

    values = {
        "event_id": "TICK-1",
        "stream_message_id": "1-0",
        "bid_price_1": Decimal("14598"),
        "bid_volume_1": 3,
        "ask_price_1": Decimal("14599"),
        "ask_volume_1": 2,
        "event_time": datetime(2026, 7, 23, 9, tzinfo=timezone.utc),
        "sequence_id": 1,
    }
    values.update(overrides)
    return MatchingMarketData(**values)


def make_order(**overrides) -> MatchingOrder:
    """创建纯撮合订单快照。"""

    values = {
        "order_id": "O-1",
        "direction": OrderDirection.BUY,
        "offset_flag": OffsetFlag.OPEN,
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("14600"),
        "remaining_volume": 5,
    }
    values.update(overrides)
    return MatchingOrder(**values)


@pytest.mark.parametrize("limit_price", [Decimal("14599"), Decimal("14600")])
def test_buy_equal_or_higher_than_ask_matches_at_ask(limit_price):
    result = VnMatchingEngine().match(
        make_order(limit_price=limit_price),
        make_market(),
    )

    assert result.matched is True
    assert result.fill_price == Decimal("14599")
    assert result.fill_volume == 2


def test_buy_below_ask_does_not_match():
    result = VnMatchingEngine().match(
        make_order(limit_price=Decimal("14598")),
        make_market(),
    )

    assert result.matched is False
    assert result.reason == "BUY_LIMIT_NOT_REACHED"


@pytest.mark.parametrize("limit_price", [Decimal("14597"), Decimal("14598")])
def test_sell_equal_or_lower_than_bid_matches_at_bid(limit_price):
    result = VnMatchingEngine().match(
        make_order(
            direction=OrderDirection.SELL,
            limit_price=limit_price,
        ),
        make_market(),
    )

    assert result.matched is True
    assert result.fill_price == Decimal("14598")
    assert result.fill_volume == 3


def test_sell_above_bid_does_not_match():
    result = VnMatchingEngine().match(
        make_order(
            direction=OrderDirection.SELL,
            limit_price=Decimal("14599"),
        ),
        make_market(),
    )

    assert result.matched is False
    assert result.reason == "SELL_LIMIT_NOT_REACHED"


@pytest.mark.parametrize(
    ("order_overrides", "market_overrides", "reason"),
    [
        ({}, {"ask_price_1": None}, "INVALID_ASK_PRICE"),
        ({}, {"ask_volume_1": 0}, "NO_ASK_VOLUME"),
        (
            {"direction": OrderDirection.SELL},
            {"bid_price_1": None},
            "INVALID_BID_PRICE",
        ),
        (
            {"direction": OrderDirection.SELL},
            {"bid_volume_1": 0},
            "NO_BID_VOLUME",
        ),
        ({"remaining_volume": 0}, {}, "NO_REMAINING_VOLUME"),
    ],
)
def test_invalid_market_or_empty_order_does_not_match(
    order_overrides,
    market_overrides,
    reason,
):
    result = VnMatchingEngine().match(
        make_order(**order_overrides),
        make_market(**market_overrides),
    )

    assert result.matched is False
    assert result.fill_price is None
    assert result.fill_volume == 0
    assert result.reason == reason


def test_displayed_volume_smaller_than_order_supports_partial_fill():
    result = VnMatchingEngine().match(
        make_order(remaining_volume=10),
        make_market(ask_volume_1=3),
    )

    assert result.fill_volume == 3


def test_displayed_volume_larger_than_order_only_fills_remaining_volume():
    result = VnMatchingEngine().match(
        make_order(remaining_volume=2),
        make_market(ask_volume_1=10),
    )

    assert result.fill_volume == 2
    assert result.fill_volume <= 2


def test_fill_price_remains_decimal_and_result_contains_engine_metadata():
    result = VnMatchingEngine().match(
        make_order(remaining_volume=1),
        make_market(
            ask_price_1=Decimal("14599.123456"),
            ask_volume_1=10,
        ),
    )

    assert result.fill_price == Decimal("14599.123456")
    assert isinstance(result.fill_price, Decimal)
    assert result.engine_name == "VN"
    assert result.engine_version == "1.0"


def test_engine_does_not_mutate_input_snapshots():
    order = make_order()
    market = make_market()
    order_before = replace(order)
    market_before = replace(market)

    VnMatchingEngine().match(order, market)

    assert order == order_before
    assert market == market_before


@pytest.mark.parametrize(
    "offset_flag",
    [
        OffsetFlag.CLOSE,
        OffsetFlag.CLOSE_TODAY,
        OffsetFlag.CLOSE_YESTERDAY,
    ],
)
def test_close_offsets_can_reuse_core_price_matching(offset_flag):
    """核心算法不开放平仓业务，只证明价格撮合逻辑不再与 OPEN 耦合。"""

    result = VnMatchingEngine().match(
        make_order(offset_flag=offset_flag),
        make_market(),
    )

    assert result.matched is True
    assert result.fill_price == Decimal("14599")


def test_vn_orders_independently_receive_full_displayed_liquidity():
    engine = VnMatchingEngine()
    market = make_market(ask_volume_1=3)

    results = [
        engine.match(make_order(order_id=f"O-{index}"), market)
        for index in range(2)
    ]

    # VN 模式不共享盘口量，两笔订单都可以各自成交 3 手。
    assert [item.fill_volume for item in results] == [3, 3]
