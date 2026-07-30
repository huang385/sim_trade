from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.common.exceptions import (
    BusinessRuleError,
    BusinessValidationError,
    ResourceConflictError,
)
from app.enums.order_enums import OffsetFlag, OrderDirection, OrderType
from app.schemas.order_schema import OrderCreateRequest
from app.services.order_validation_service import OrderValidationService


def make_request(**overrides):
    values = {
        "client_order_id": "CLIENT-1",
        "account_id": "A001",
        "exchange_id": "SHFE",
        "symbol": "RB2610",
        "direction": OrderDirection.BUY,
        "offset_flag": OffsetFlag.OPEN,
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("3500"),
        "volume": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_instrument(**overrides):
    values = {
        "is_active": True,
        "min_volume": 1,
        "max_volume": 100,
        "price_tick": Decimal("1"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_existing_order(**overrides):
    values = {
        "account_id": "A001",
        "exchange_id": "SHFE",
        "symbol": "RB2610",
        "direction": "BUY",
        "offset_flag": "OPEN",
        "order_type": "LIMIT",
        "limit_price": Decimal("3500.000000"),
        "total_volume": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "direction",
    [OrderDirection.BUY, OrderDirection.SELL],
)
def test_valid_open_limit_order(direction):
    OrderValidationService.validate_open_order(
        request=make_request(direction=direction),
        instrument=make_instrument(),
    )


def test_schema_normalizes_exchange_and_symbol():
    request = OrderCreateRequest(
        client_order_id=" CLIENT-1 ",
        account_id=" A001 ",
        exchange_id=" shfe ",
        symbol=" rb2610 ",
        direction="BUY",
        offset_flag="OPEN",
        limit_price="3500",
        volume=2,
    )

    assert request.client_order_id == "CLIENT-1"
    assert request.account_id == "A001"
    assert request.exchange_id == "SHFE"
    assert request.symbol == "RB2610"


def test_rejects_missing_instrument():
    with pytest.raises(BusinessRuleError) as exc_info:
        OrderValidationService.validate_open_order(
            request=make_request(),
            instrument=None,
        )

    assert exc_info.value.error_code == "INSTRUMENT_NOT_FOUND"


def test_rejects_inactive_instrument():
    with pytest.raises(BusinessRuleError) as exc_info:
        OrderValidationService.validate_open_order(
            request=make_request(),
            instrument=make_instrument(is_active=False),
        )

    assert exc_info.value.error_code == "INSTRUMENT_INACTIVE"


def test_rejects_non_open_order():
    with pytest.raises(BusinessValidationError) as exc_info:
        OrderValidationService.validate_open_order(
            request=make_request(offset_flag=OffsetFlag.CLOSE),
            instrument=make_instrument(),
        )

    assert exc_info.value.error_code == "UNSUPPORTED_OFFSET_FLAG"


def test_rejects_unsupported_order_type():
    with pytest.raises(BusinessValidationError) as exc_info:
        OrderValidationService.validate_open_order(
            request=make_request(order_type="MARKET"),
            instrument=make_instrument(),
        )

    assert exc_info.value.error_code == "UNSUPPORTED_ORDER_TYPE"


def test_rejects_price_tick_mismatch():
    with pytest.raises(BusinessValidationError) as exc_info:
        OrderValidationService.validate_open_order(
            request=make_request(limit_price=Decimal("3500.5")),
            instrument=make_instrument(price_tick=Decimal("1")),
        )

    assert exc_info.value.error_code == "PRICE_TICK_MISMATCH"


def test_rejects_volume_below_minimum():
    with pytest.raises(BusinessValidationError) as exc_info:
        OrderValidationService.validate_open_order(
            request=make_request(volume=1),
            instrument=make_instrument(min_volume=2),
        )

    assert exc_info.value.error_code == "VOLUME_BELOW_MINIMUM"


def test_rejects_volume_above_maximum():
    with pytest.raises(BusinessValidationError) as exc_info:
        OrderValidationService.validate_open_order(
            request=make_request(volume=11),
            instrument=make_instrument(max_volume=10),
        )

    assert exc_info.value.error_code == "VOLUME_ABOVE_MAXIMUM"


def test_rejects_invalid_price_tick():
    with pytest.raises(BusinessValidationError) as exc_info:
        OrderValidationService.validate_price_tick(
            price=Decimal("3500"),
            price_tick=Decimal("0"),
        )

    assert exc_info.value.error_code == "INVALID_PRICE_TICK"


def test_identical_idempotent_order_request_is_accepted():
    OrderValidationService.validate_idempotent_order_request(
        existing_order=make_existing_order(
            exchange_id="shfe",
            symbol="rb2610",
        ),
        request=make_request(limit_price=Decimal("3500.0000004")),
    )


@pytest.mark.parametrize(
    ("request_override", "existing_override"),
    [
        ({"account_id": "A002"}, {}),
        ({"exchange_id": "DCE"}, {}),
        ({"symbol": "JD2609"}, {}),
        ({"direction": OrderDirection.SELL}, {}),
        ({"offset_flag": OffsetFlag.CLOSE}, {}),
        ({"order_type": "MARKET"}, {}),
        ({"limit_price": Decimal("3501")}, {}),
        ({"volume": 3}, {}),
    ],
)
def test_reused_idempotency_key_rejects_changed_business_fields(
    request_override,
    existing_override,
):
    with pytest.raises(ResourceConflictError) as exc_info:
        OrderValidationService.validate_idempotent_order_request(
            existing_order=make_existing_order(**existing_override),
            request=make_request(**request_override),
        )

    assert exc_info.value.error_code == "IDEMPOTENCY_KEY_REUSED"
