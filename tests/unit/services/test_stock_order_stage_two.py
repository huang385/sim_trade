from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.common.exceptions import (
    BusinessRuleError,
    BusinessValidationError,
    DataAccessError,
    ResourceConflictError,
)
from app.enums.order_enums import OrderDirection
from app.repositories.fee_rule_item_repository import FeeRuleItemRepository
from app.schemas.order_schema import OrderCreateRequest, StockOrderCreateRequest
from app.services.account_access_scope import AccountAccessScope
from app.services.cash_security_order_event_service import (
    CashSecurityOrderEventService,
)
from app.services.fee_calculator import FeeCalculator, StockFeeComponent
from app.services.order_validation_service import OrderValidationService
from app.services.product_strategy_registry import resolve_product_strategy
from app.services.stock_order_cancellation_service import StockOrderCancellationService
from app.services.stock_order_service import StockOrderService
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

    with pytest.raises(BusinessValidationError) as exceeded:
        service.resolve_and_validate(
            object(),
            instrument=_instrument(),
            request=_request(limit_price="111"),
            trading_day=date.today(),
        )
    assert exceeded.value.error_code == "STOCK_PRICE_LIMIT_EXCEEDED"
    with pytest.raises(BusinessValidationError) as lot_mismatch:
        service.validate_buy(request=_request(volume=101), rule=reference.rule)
    assert lot_mismatch.value.error_code == "STOCK_BUY_LOT_MISMATCH"


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
    with pytest.raises(BusinessRuleError) as insufficient:
        service.validate_sell(
            request=_request(direction="SELL", volume=200),
            rule=reference.rule,
            position=position,
        )
    assert insufficient.value.error_code == "STOCK_AVAILABLE_VOLUME_INSUFFICIENT"


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


def test_stock_outbox_event_registers_a_cash_security_active_order():
    order = SimpleNamespace(
        order_id="O", account_id="A", exchange_id="SSE", symbol="600519",
        instrument_type="STOCK", status="ACCEPTED", remaining_volume=100,
    )
    orders = Mock()
    orders.get_by_order_id.return_value = order
    active_index = Mock()
    active_index.add_active_order.return_value = True
    service = CashSecurityOrderEventService(
        order_repository=orders,
        active_order_index=active_index,
        processed_ttl_seconds=60,
    )
    result = service.process(
        Mock(),
        {
            "event_id": "EVENT-1",
            "event_type": "STOCK_ORDER_ACCEPTED",
            "payload": (
                '{"event_type":"STOCK_ORDER_ACCEPTED","event_id":"EVENT-1",'
                '"account_id":"A","account_type":"SECURITIES_CASH",'
                '"order_id":"O","instrument_type":"STOCK",'
                '"exchange_id":"SSE","symbol":"600519"}'
            ),
        },
    )

    assert result.action == "REGISTERED"
    active_index.add_active_order.assert_called_once_with(
        order, event_id="EVENT-1", processed_ttl_seconds=60
    )


def test_stock_cancel_event_removes_cash_security_active_order():
    order = SimpleNamespace(
        order_id="O", account_id="A", exchange_id="SSE", symbol="600519",
        instrument_type="STOCK", status="CANCELLED", remaining_volume=0,
    )
    orders = Mock()
    orders.get_by_order_id.return_value = order
    active_index = Mock()
    active_index.remove_active_order.return_value = True
    service = CashSecurityOrderEventService(
        order_repository=orders,
        active_order_index=active_index,
        processed_ttl_seconds=60,
    )

    result = service.process(
        Mock(),
        {
            "event_id": "EVENT-2",
            "event_type": "STOCK_ORDER_CANCELLED",
            "payload": (
                '{"event_type":"STOCK_ORDER_CANCELLED","event_id":"EVENT-2",'
                '"account_id":"A","account_type":"SECURITIES_CASH",'
                '"order_id":"O","instrument_type":"STOCK",'
                '"exchange_id":"SSE","symbol":"600519"}'
            ),
        },
    )

    assert result.action == "REMOVED"
    active_index.remove_active_order.assert_called_once_with(
        order_id="O", account_id="A", exchange_id="SSE", symbol="600519",
        event_id="EVENT-2", processed_ttl_seconds=60,
    )


def _existing_order(*, instrument_type="STOCK", offset_flag=None):
    return SimpleNamespace(
        instrument_type=instrument_type,
        account_id="STOCK-A",
        exchange_id="SSE",
        symbol="600519",
        direction="BUY",
        offset_flag=offset_flag,
        order_type="LIMIT",
        submitted_limit_price=Decimal("100.000000"),
        limit_price=Decimal("100.000000"),
        total_volume=100,
    )


def test_stock_idempotency_compares_missing_offset_flag_safely():
    OrderValidationService.validate_idempotent_order_request(
        existing_order=_existing_order(), request=_request()
    )


def test_cross_product_idempotency_key_is_rejected_before_field_comparison():
    with pytest.raises(ResourceConflictError) as stock_conflict:
        OrderValidationService.validate_idempotent_order_request(
            existing_order=_existing_order(instrument_type="FUTURES", offset_flag="OPEN"),
            request=_request(),
        )
    assert stock_conflict.value.error_code == "IDEMPOTENCY_KEY_REUSED"

    with pytest.raises(ResourceConflictError) as derivative_conflict:
        OrderValidationService.validate_idempotent_order_request(
            existing_order=_existing_order(),
            request=OrderCreateRequest(
                client_order_id="STOCK-1",
                account_id="STOCK-A",
                exchange_id="SSE",
                symbol="600519",
                direction="BUY",
                offset_flag="OPEN",
                order_type="LIMIT",
                limit_price="100",
                volume=100,
            ),
        )
    assert derivative_conflict.value.error_code == "IDEMPOTENCY_KEY_REUSED"


def test_stock_service_idempotent_retry_does_not_freeze_or_emit_again(monkeypatch):
    import app.services.stock_order_service as module

    monkeypatch.setattr(module.settings, "stock_order_entry_enabled", True)
    existing = _existing_order()
    existing.order_id = "SO-EXISTING"
    account = SimpleNamespace(account_type="STOCK")
    account_repository = Mock()
    account_repository.get_by_account_id.return_value = account
    order_repository = Mock()
    order_repository.get_by_client_order_id.return_value = existing
    snapshots = Mock()
    outbox = Mock()
    db = Mock()
    service = StockOrderService(
        account_repository=account_repository,
        order_repository=order_repository,
        snapshot_repository=snapshots,
        outbox_repository=outbox,
    )

    result = service.create_order(
        db=db, request=_request(), access_scope=AccountAccessScope.admin()
    )

    assert result is existing
    db.expunge.assert_called_once_with(existing)
    db.commit.assert_called_once()
    account_repository.get_by_account_id_for_update.assert_not_called()
    snapshots.add_all.assert_not_called()
    outbox.create_event.assert_not_called()


def test_duplicate_highest_priority_stock_fee_rule_fails_closed():
    first = SimpleNamespace(fee_type="BROKER_COMMISSION", id=1)
    second = SimpleNamespace(fee_type="BROKER_COMMISSION", id=2)
    db = Mock()
    db.execute.return_value.all.return_value = [(first, 1), (second, 1)]

    with pytest.raises(DataAccessError) as ambiguous:
        FeeRuleItemRepository.resolve_stock_components(
            db,
            instrument_id=1,
            product_id="600519",
            exchange_id="SSE",
            direction="BUY",
            trading_day=date.today(),
        )

    assert ambiguous.value.error_code == "STOCK_FEE_COMPONENT_AMBIGUOUS"


def test_stock_sell_cancel_rejects_partial_frozen_volume_and_rolls_back(monkeypatch):
    import app.services.stock_order_cancellation_service as module

    monkeypatch.setattr(module.settings, "stock_order_entry_enabled", True)
    order = SimpleNamespace(
        instrument_type="STOCK",
        account_id="STOCK-A",
        status="ACCEPTED",
        remaining_volume=100,
        frozen_cash=Decimal("0"),
        frozen_commission=Decimal("0"),
        frozen_margin=Decimal("0"),
        frozen_position_volume=20,
        direction="SELL",
        cancelled_volume=0,
        traded_volume=0,
    )
    account = SimpleNamespace(account_type="STOCK")
    orders = Mock()
    orders.get_by_order_id_for_update.return_value = order
    accounts = Mock()
    accounts.get_by_account_id_for_update.return_value = account
    outbox = Mock()
    db = Mock()
    service = StockOrderCancellationService(
        order_repository=orders,
        account_repository=accounts,
        outbox_repository=outbox,
    )

    with pytest.raises(DataAccessError) as inconsistent:
        service.cancel_order(
            db=db,
            order_id="SO-1",
            request=module.OrderCancelRequest(account_id="STOCK-A"),
            access_scope=AccountAccessScope.admin(),
        )

    assert inconsistent.value.error_code == "STOCK_CANCEL_STATE_INCONSISTENT"
    assert order.status == "ACCEPTED"
    db.rollback.assert_called_once()
    outbox.create_event.assert_not_called()
