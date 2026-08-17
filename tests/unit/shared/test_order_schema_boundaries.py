from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.enums.order_enums import OffsetFlag
from app.schemas.order_schema import (
    CommonOrderCreateRequest,
    DerivativeOrderCreateRequest,
    OrderCreateRequest,
    OrderResponse,
)
from app.schemas.trade_schema import TradeResponse


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


@pytest.mark.parametrize("payload", [{}, {"offset_flag": None}])
def test_derivative_request_rejects_missing_or_null_offset_flag(payload):
    with pytest.raises(ValidationError):
        OrderCreateRequest(**COMMON_FIELDS, **payload)


@pytest.mark.parametrize(
    "offset_flag",
    [
        OffsetFlag.OPEN,
        OffsetFlag.CLOSE,
        OffsetFlag.CLOSE_TODAY,
        OffsetFlag.CLOSE_YESTERDAY,
    ],
)
def test_derivative_request_accepts_all_legal_offset_flags(offset_flag):
    request = OrderCreateRequest(**COMMON_FIELDS, offset_flag=offset_flag)

    assert request.offset_flag == offset_flag


def test_order_and_trade_responses_allow_stock_null_offset_flag():
    assert OrderResponse.model_fields["offset_flag"].annotation == (
        OffsetFlag | None
    )
    assert TradeResponse.model_fields["offset_flag"].annotation == str | None


def test_client_product_type_cannot_become_strategy_input():
    request = OrderCreateRequest(
        **COMMON_FIELDS,
        offset_flag="OPEN",
        instrument_type="STOCK",
    )

    assert not hasattr(request, "instrument_type")
