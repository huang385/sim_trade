from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.common.exceptions import BusinessValidationError
from app.common.time_utils import utc_now
from app.enums.order_enums import OffsetFlag, OrderDirection, OrderType
from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType
from app.schemas.order_schema import OrderCreateRequest
from app.services.live_market_snapshot_service import LiveMatchingEvent
from app.services.market_tick_matching_service import ParsedMarketTickEvent
from app.services.order_price_resolver import OrderPriceResolver


TRADING_DAY = date(2026, 8, 10)


def make_request(order_type, direction=OrderDirection.BUY, limit_price=None):
    return OrderCreateRequest(
        client_order_id=f"TEST-{order_type.value}-{direction.value}",
        account_id="A001",
        exchange_id="DCE",
        symbol="JD2609",
        direction=direction,
        offset_flag=OffsetFlag.OPEN,
        order_type=order_type,
        limit_price=limit_price,
        volume=5,
    )


def make_event(**overrides):
    values = {
        "source_event_id": "TICK-1",
        "source": "YMM_LIVE_DATA",
        "ingest_type": MarketTickIngestType.LIVE_CALLBACK,
        "order_book_id": "JD2609",
        "exchange_id": "DCE",
        "symbol": "JD2609",
        "trading_day": TRADING_DAY,
        "event_time": utc_now(),
        "sequence_id": 7,
        "last_price": Decimal("4354"),
        "cumulative_volume": 100,
        "bid_price_1": Decimal("4353"),
        "bid_volume_1": 3,
        "ask_price_1": Decimal("4355"),
        "ask_volume_1": 2,
    }
    values.update(overrides)
    tick = MarketTick(**values)
    return LiveMatchingEvent(
        stream_message_id="1-0",
        parsed_event=ParsedMarketTickEvent(
            event_id=tick.source_event_id,
            exchange_id=tick.exchange_id,
            symbol=tick.symbol,
            tick=tick,
        ),
    )


def make_resolver(event):
    snapshots = Mock()
    snapshots.get_matching_event.return_value = event
    return (
        OrderPriceResolver(
            live_market_snapshot_service=snapshots,
            market_max_slippage_rate=Decimal("0.02"),
        ),
        snapshots,
    )


@pytest.mark.parametrize(
    ("direction", "expected"),
    [(OrderDirection.BUY, Decimal("4355")), (OrderDirection.SELL, Decimal("4353"))],
)
def test_counterparty_uses_acceptance_opposite_price(direction, expected):
    resolver, snapshots = make_resolver(make_event())
    result = resolver.resolve(
        request=make_request(OrderType.COUNTERPARTY, direction),
        order_book_id="JD2609",
        price_tick=Decimal("1"),
        trading_day=TRADING_DAY,
    )
    assert result.resolved_price == expected
    assert result.bid1 == Decimal("4353")
    assert result.ask1 == Decimal("4355")
    snapshots.get_matching_event.assert_called_once()


def test_last_price_is_fixed_from_one_snapshot():
    resolver, snapshots = make_resolver(make_event())
    result = resolver.resolve(
        request=make_request(OrderType.LAST),
        order_book_id="JD2609",
        price_tick=Decimal("1"),
        trading_day=TRADING_DAY,
    )
    snapshots.get_matching_event.return_value = make_event(last_price=Decimal("4400"))
    assert result.resolved_price == Decimal("4354")
    assert result.last_price == Decimal("4354")


@pytest.mark.parametrize(
    ("direction", "expected"),
    [(OrderDirection.BUY, Decimal("4443")), (OrderDirection.SELL, Decimal("4265"))],
)
def test_market_price_uses_directional_two_percent_protection(direction, expected):
    resolver, _ = make_resolver(make_event())
    result = resolver.resolve(
        request=make_request(OrderType.MARKET, direction),
        order_book_id="JD2609",
        price_tick=Decimal("1"),
        trading_day=TRADING_DAY,
    )
    assert result.resolved_price == expected
    assert result.market_protection_price == expected


@pytest.mark.parametrize(
    ("order_type", "field", "code"),
    [
        (OrderType.COUNTERPARTY, "ask_price_1", "ASK1_MISSING"),
        (OrderType.LAST, "last_price", "LAST_PRICE_MISSING"),
        (OrderType.MARKET, "ask_price_1", "ASK1_MISSING"),
    ],
)
def test_required_snapshot_price_must_exist(order_type, field, code):
    resolver, _ = make_resolver(make_event(**{field: None}))
    with pytest.raises(BusinessValidationError) as exc_info:
        resolver.resolve(
            request=make_request(order_type),
            order_book_id="JD2609",
            price_tick=Decimal("1"),
            trading_day=TRADING_DAY,
        )
    assert exc_info.value.error_code == code


def test_old_snapshot_from_current_subscription_is_accepted():
    # 低活跃合约盘口可能几分钟才更新一次；只要行情来自当前活跃订阅
    # （订阅代次校验由 LiveMarketSnapshotService 负责），不再按年龄拒绝。
    resolver, _ = make_resolver(
        make_event(event_time=utc_now() - timedelta(minutes=5))
    )
    result = resolver.resolve(
        request=make_request(OrderType.COUNTERPARTY),
        order_book_id="JD2609",
        price_tick=Decimal("1"),
        trading_day=TRADING_DAY,
    )
    assert result.resolved_price == Decimal("4355")


def test_future_timestamp_snapshot_is_rejected():
    resolver, _ = make_resolver(
        make_event(event_time=utc_now() + timedelta(seconds=5))
    )
    with pytest.raises(BusinessValidationError) as exc_info:
        resolver.resolve(
            request=make_request(OrderType.COUNTERPARTY),
            order_book_id="JD2609",
            price_tick=Decimal("1"),
            trading_day=TRADING_DAY,
        )
    assert exc_info.value.error_code == "ORDER_PRICE_MARKET_DATA_INVALID"


def test_limit_does_not_read_market():
    resolver, snapshots = make_resolver(make_event())
    result = resolver.resolve(
        request=make_request(OrderType.LIMIT, limit_price=Decimal("4350")),
        order_book_id="JD2609",
        price_tick=Decimal("1"),
        trading_day=TRADING_DAY,
    )
    assert result.resolved_price == Decimal("4350")
    snapshots.get_matching_event.assert_not_called()


def test_limit_requires_client_price_and_market_rejects_client_price():
    with pytest.raises(ValidationError):
        make_request(OrderType.LIMIT, limit_price=None)
    with pytest.raises(ValidationError):
        make_request(OrderType.MARKET, limit_price=Decimal("999999999"))
