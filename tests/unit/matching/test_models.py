from dataclasses import FrozenInstanceError, fields
from decimal import Decimal

import pytest

from app.enums.order_enums import OffsetFlag, OrderDirection, OrderType
from app.matching.models import MatchResult, MatchingMarketData, MatchingOrder


def _field_names(model_type) -> set[str]:
    """返回 dataclass 的公开字段名，防止基础设施字段重新混入纯撮合模型。"""

    return {item.name for item in fields(model_type)}


def test_matching_order_only_contains_algorithm_inputs():
    assert _field_names(MatchingOrder) == {
        "direction",
        "offset_flag",
        "order_type",
        "limit_price",
        "remaining_volume",
    }
    assert "order_id" not in _field_names(MatchingOrder)


def test_market_data_only_contains_top_of_book_inputs():
    names = _field_names(MatchingMarketData)

    assert names == {
        "bid_price_1",
        "bid_volume_1",
        "ask_price_1",
        "ask_volume_1",
    }
    assert names.isdisjoint(
        {"event_id", "stream_message_id", "event_time", "sequence_id"}
    )


def test_match_result_only_contains_calculation_output():
    names = _field_names(MatchResult)

    assert names == {
        "matched",
        "fill_price",
        "fill_volume",
        "reason",
        "engine_name",
        "engine_version",
    }
    assert names.isdisjoint(
        {
            "order_id",
            "market_event_id",
            "market_stream_message_id",
            "tick_event_time",
            "tick_sequence_id",
        }
    )


def test_matching_inputs_are_frozen_and_prices_remain_decimal():
    order = MatchingOrder(
        direction=OrderDirection.BUY,
        offset_flag=OffsetFlag.OPEN,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("3500.5"),
        remaining_volume=2,
    )
    market = MatchingMarketData(
        bid_price_1=Decimal("3499.5"),
        bid_volume_1=3,
        ask_price_1=Decimal("3500.5"),
        ask_volume_1=4,
    )

    assert isinstance(order.limit_price, Decimal)
    assert isinstance(market.bid_price_1, Decimal)
    assert isinstance(market.ask_price_1, Decimal)
    with pytest.raises(FrozenInstanceError):
        order.remaining_volume = 1
    with pytest.raises(FrozenInstanceError):
        market.ask_volume_1 = 1
