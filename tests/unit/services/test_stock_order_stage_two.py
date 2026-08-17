from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.enums.order_enums import OrderDirection
from app.schemas.order_schema import StockOrderCreateRequest
from app.services.accepted_order_event_service import AcceptedOrderEventService
from app.services.fee_calculator import FeeCalculator, StockFeeComponent
from app.services.product_strategy_registry import resolve_product_strategy
from app.services.stock_order_validation_service import StockOrderValidationService


def _request(**overrides):
    values = {
        "client_order_id": "STOCK-1",
        "account_id": "STOCK-A",
        "exchange_id": "SSE",
        "symbol": "600519",
        "direction": "BUY",
        "order_type": "LIMIT",
        "limit_price": "100.00",
        "volume": 100,
    }
    values.update(overrides)
    return StockOrderCreateRequest(**values)


def _instrument():
    return SimpleNamespace(
        id=1,
        instrument_type="STOCK",
        market_type="STOCK",
        is_active=True,
        is_tradeable=True,
        min_volume=1,
        max_volume=1_000_000,
        price_tick=Decimal("0.01"),
    )


def _validator():
    rule = SimpleNamespace(
        price_limit_type="RATIO",
        buy_lot_size=100,
        buy_volume_must_be_multiple=True,
        sell_min_unit=100,
        sell_odd_lot_allowed=False,
    )
    fact = SimpleNamespace(
        is_suspended=False,
        is_tradeable=True,
        upper_limit_price=Decimal("110"),
        lower_limit_price=Decimal("90"),
    )
    return StockOrderValidationService(
        rule_repository=SimpleNamespace(
            resolve_for_trading_day=lambda *args, **kwargs: rule
        ),
        fact_repository=SimpleNamespace(get=lambda *args, **kwargs: fact),
    )


def test_stock_request_is_strictly_separate_from_derivative_offset_flag():
    assert _request().order_type == "LIMIT"
    with pytest.raises(ValidationError):
        _request(offset_flag="OPEN")
    with pytest.raises(ValidationError):
        _request(order_type="MARKET")


def test_stock_product_strategy_is_registered_without_matching_fallback():
    strategy = resolve_product_strategy("STOCK")

    assert strategy.family.value == "STOCKS"
    assert strategy.is_option is False


def test_stock_validation_uses_daily_limits_and_configured_lots():
    service = _validator()
    reference = service.resolve_and_validate(
        object(), instrument=_instrument(), request=_request(), trading_day=date.today()
    )
    service.validate_buy(request=_request(), rule=reference.rule)

    with pytest.raises(Exception, match="涨跌停"):
        service.resolve_and_validate(
            object(),
            instrument=_instrument(),
            request=_request(limit_price="111"),
            trading_day=date.today(),
        )
    with pytest.raises(Exception, match="整数倍"):
        service.validate_buy(request=_request(volume=101), rule=reference.rule)


def test_stock_sell_validation_uses_available_volume_and_long_position():
    service = _validator()
    reference = service.resolve_and_validate(
        object(), instrument=_instrument(), request=_request(), trading_day=date.today()
    )
    position = SimpleNamespace(direction="LONG", available_volume=100)
    service.validate_sell(
        request=_request(direction=OrderDirection.SELL),
        rule=reference.rule,
        position=position,
    )
    with pytest.raises(Exception, match="可卖数量不足"):
        service.validate_sell(
            request=_request(direction="SELL", volume=200),
            rule=reference.rule,
            position=position,
        )


def test_stock_fee_components_are_calculated_individually_with_minimums():
    components = (
        StockFeeComponent(
            fee_type="BROKER_COMMISSION",
            rule_item_id=1,
            rule_version="V1",
            direction="BUY",
            calculation_type="BY_AMOUNT",
            commission_parameter=Decimal("0.001"),
            minimum_fee=Decimal("5"),
            aggregation_scope="ORDER",
            contract_multiplier=Decimal("1"),
            data_source="TEST",
        ),
        StockFeeComponent(
            fee_type="TRANSFER_FEE",
            rule_item_id=2,
            rule_version="V1",
            direction="BUY",
            calculation_type="BY_VOLUME",
            commission_parameter=Decimal("0.01"),
            minimum_fee=Decimal("0"),
            aggregation_scope="TRADE",
            contract_multiplier=Decimal("1"),
            data_source="TEST",
        ),
    )

    assert FeeCalculator.calculate_stock_components(
        price=Decimal("10"), volume=100, components=components
    ) == Decimal("6.000000")


def test_stock_outbox_events_are_acknowledged_without_matching_index():
    event = AcceptedOrderEventService.parse_event(
        {
            "event_id": "EVENT-1",
            "event_type": "STOCK_ORDER_ACCEPTED",
            "payload": (
                '{"event_type":"STOCK_ORDER_ACCEPTED","account_id":"A",'
                '"order_id":"O","exchange_id":"SSE","symbol":"600519"}'
            ),
        }
    )

    assert event.event_type == "STOCK_ORDER_ACCEPTED"
