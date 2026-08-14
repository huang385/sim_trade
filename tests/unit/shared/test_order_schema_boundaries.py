from decimal import Decimal

from app.schemas.order_schema import (
    CommonOrderCreateRequest,
    DerivativeOrderCreateRequest,
    OrderCreateRequest,
)


COMMON_FIELDS = {
    "client_order_id": "C-1",
    "account_id": "A-1",
    "exchange_id": "SHFE",
    "symbol": "RB2610",
    "direction": "BUY",
    "order_type": "LIMIT",
    "limit_price": Decimal("3500"),
    "volume": 1,
}


def test_common_order_fields_do_not_force_derivative_offset():
    request = CommonOrderCreateRequest(**COMMON_FIELDS)

    assert not hasattr(request, "offset_flag")


def test_legacy_order_request_remains_derivative_compatible():
    request = OrderCreateRequest(**COMMON_FIELDS, offset_flag="OPEN")

    assert isinstance(request, DerivativeOrderCreateRequest)
    assert request.offset_flag.value == "OPEN"


def test_client_product_type_cannot_become_strategy_input():
    request = OrderCreateRequest(
        **COMMON_FIELDS,
        offset_flag="OPEN",
        instrument_type="STOCK",
    )

    assert not hasattr(request, "instrument_type")
