from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.common.exceptions import ResourceConflictError, ResourceNotFoundError
from app.core.database import get_db
from app.main import app
from app.services.order_cancellation_service import (
    get_order_cancellation_service,
)
from app.services.account_access_scope import AccountAccessScope
from tests.api_auth_helpers import install_admin_auth_overrides


def make_order(**overrides):
    now = datetime(2026, 7, 24, 1, tzinfo=timezone.utc)
    values = {
        "order_id": "O-1",
        "client_order_id": "C-1",
        "account_id": "A001",
        "order_book_id": "RB2610",
        "exchange_id": "SHFE",
        "symbol": "RB2610",
        "trading_day": date(2026, 7, 24),
        "direction": "BUY",
        "offset_flag": "OPEN",
        "order_type": "LIMIT",
        "limit_price": Decimal("3500"),
        "total_volume": 10,
        "traded_volume": 0,
        "remaining_volume": 0,
        "cancelled_volume": 10,
        "average_price": None,
        "frozen_margin": Decimal("0"),
        "frozen_commission": Decimal("0"),
        "frozen_position_volume": 0,
        "status": "CANCELLED",
        "submit_status": "ACCEPTED",
        "reject_code": None,
        "reject_message": None,
        "created_at": now,
        "accepted_at": now,
        "cancelled_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def cancel_api():
    service = Mock()
    database_session = Mock()

    def override_db():
        yield database_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_order_cancellation_service] = lambda: service
    install_admin_auth_overrides()
    try:
        yield TestClient(app), service, database_session
    finally:
        app.dependency_overrides.clear()


def test_cancel_api_returns_cancelled_order_and_strips_account(cancel_api):
    client, service, database_session = cancel_api
    service.cancel_order.return_value = make_order()

    response = client.post(
        "/api/orders/O-1/cancel",
        json={"account_id": "  A001  "},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "CANCELLED"
    assert body["cancelled_volume"] == 10
    assert body["remaining_volume"] == 0
    assert body["cancelled_at"] is not None
    call = service.cancel_order.call_args.kwargs
    assert call["db"] is database_session
    assert call["order_id"] == "O-1"
    assert call["request"].account_id == "A001"
    assert call["access_scope"] == AccountAccessScope.admin()


def test_cancel_api_returns_partially_cancelled_and_is_repeatable(cancel_api):
    client, service, _ = cancel_api
    service.cancel_order.return_value = make_order(
        status="PARTIALLY_CANCELLED",
        traded_volume=3,
        cancelled_volume=7,
        average_price=Decimal("3499"),
    )

    first = client.post(
        "/api/orders/O-1/cancel",
        json={"account_id": "A001"},
    )
    second = client.post(
        "/api/orders/O-1/cancel",
        json={"account_id": "A001"},
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == "PARTIALLY_CANCELLED"
    assert first.json()["traded_volume"] == 3
    assert first.json()["cancelled_volume"] == 7


@pytest.mark.parametrize(
    "payload",
    [{}, {"account_id": ""}, {"account_id": "   "}],
)
def test_cancel_api_validates_account_id(cancel_api, payload):
    client, service, _ = cancel_api

    response = client.post("/api/orders/O-1/cancel", json=payload)

    assert response.status_code == 422
    service.cancel_order.assert_not_called()


@pytest.mark.parametrize(
    ("error", "status_code", "error_code"),
    [
        (
            ResourceNotFoundError(
                "订单不存在",
                error_code="ORDER_NOT_FOUND",
            ),
            404,
            "ORDER_NOT_FOUND",
        ),
        (
            ResourceConflictError(
                "订单不属于指定账户",
                error_code="ORDER_ACCOUNT_MISMATCH",
            ),
            409,
            "ORDER_ACCOUNT_MISMATCH",
        ),
        (
            ResourceConflictError(
                "订单当前状态不允许撤销",
                error_code="ORDER_NOT_CANCELLABLE",
            ),
            409,
            "ORDER_NOT_CANCELLABLE",
        ),
    ],
)
def test_cancel_api_maps_business_errors(
    cancel_api,
    error,
    status_code,
    error_code,
):
    client, service, _ = cancel_api
    service.cancel_order.side_effect = error

    response = client.post(
        "/api/orders/O-1/cancel",
        json={"account_id": "A001"},
    )

    assert response.status_code == status_code
    assert response.json()["error_code"] == error_code
