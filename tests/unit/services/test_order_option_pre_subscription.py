from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.common.exceptions import BusinessRuleError, ServiceUnavailableError
from app.schemas.order_schema import OrderCreateRequest
from app.services.order_service import OrderService


def make_service(*, market_prices, pre_subscriptions):
    return OrderService(
        order_repository=Mock(),
        account_repository=Mock(),
        rule_query_service=Mock(),
        validation_service=Mock(),
        freeze_service=Mock(),
        margin_calculator=Mock(),
        fee_calculator=Mock(),
        option_permission_service=Mock(),
        option_market_price_service=market_prices,
        market_pre_subscription_store=pre_subscriptions,
    )


def make_request():
    return OrderCreateRequest(
        client_order_id="CLIENT-OPTION-1",
        account_id="A001",
        exchange_id="DCE",
        symbol="JD2609-C-4000",
        direction="SELL",
        offset_flag="OPEN",
        order_type="LIMIT",
        limit_price=Decimal("101"),
        volume=3,
    )


def make_rules():
    return SimpleNamespace(
        instrument=SimpleNamespace(
            order_book_id="JD2609-C-4000",
        ),
        underlying_instrument=SimpleNamespace(
            order_book_id="JD2609",
        ),
    )


def test_missing_option_prices_create_pre_subscription_and_return_retry_error():
    prices = Mock()
    prices.get_margin_prices.side_effect = BusinessRuleError(
        "行情缺失",
        error_code="OPTION_MARKET_PRICE_UNAVAILABLE",
    )
    pre_subscriptions = Mock()
    service = make_service(
        market_prices=prices,
        pre_subscriptions=pre_subscriptions,
    )
    request = make_request()
    account = SimpleNamespace(account_id="A001")
    rules = make_rules()

    with pytest.raises(ServiceUnavailableError) as caught:
        service._get_option_margin_prices(
            request=request,
            rules=rules,
            authorized_account=account,
        )

    assert caught.value.error_code == "OPTION_MARKET_DATA_PREPARING"
    pre_subscriptions.request_codes.assert_called_once_with(
        account_id="A001",
        codes={"JD2609-C-4000", "JD2609"},
    )
    service.option_permission_service.validate.assert_called_once_with(
        account=account,
        instrument=rules.instrument,
        direction=request.direction,
        offset_flag=request.offset_flag,
    )


def test_other_market_price_business_error_does_not_create_subscription():
    original = BusinessRuleError(
        "其他规则错误",
        error_code="OTHER_PRICE_ERROR",
    )
    prices = Mock()
    prices.get_margin_prices.side_effect = original
    pre_subscriptions = Mock()
    service = make_service(
        market_prices=prices,
        pre_subscriptions=pre_subscriptions,
    )

    with pytest.raises(BusinessRuleError) as caught:
        service._get_option_margin_prices(
            request=make_request(),
            rules=make_rules(),
            authorized_account=SimpleNamespace(account_id="A001"),
        )

    assert caught.value is original
    pre_subscriptions.request_codes.assert_not_called()
