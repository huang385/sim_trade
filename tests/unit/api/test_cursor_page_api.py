from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.api.order_api import get_order_service
from app.api.trade_api import get_trade_query_service
from app.core.database import get_db
from app.main import app
from tests.api_auth_helpers import install_admin_auth_overrides


def test_order_page_route_returns_protocol_and_keeps_legacy_list():
    order_service = Mock()
    order_service.list_order_page.return_value = {
        "items": [],
        "next_cursor": None,
        "has_more": False,
    }
    order_service.list_orders.return_value = []
    app.dependency_overrides[get_db] = lambda: Mock()
    app.dependency_overrides[get_order_service] = lambda: order_service
    install_admin_auth_overrides()
    try:
        client = TestClient(app)
        page = client.get(
            "/api/orders/page",
            params={"account_id": "A001", "limit": 100},
        )
        legacy = client.get(
            "/api/orders",
            params={"account_id": "A001", "limit": 100},
        )
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert page.json() == {
        "items": [],
        "next_cursor": None,
        "has_more": False,
    }
    assert legacy.status_code == 200
    assert legacy.json() == []
    order_service.get_order.assert_not_called()


def test_trade_page_route_and_limit_validation():
    trade_service = Mock()
    trade_service.list_page.return_value = {
        "items": [],
        "next_cursor": None,
        "has_more": False,
    }
    app.dependency_overrides[get_db] = lambda: Mock()
    app.dependency_overrides[
        get_trade_query_service
    ] = lambda: trade_service
    install_admin_auth_overrides()
    try:
        client = TestClient(app)
        page = client.get(
            "/api/trades/page",
            params={"account_id": "A001", "limit": 100},
        )
        too_large = client.get(
            "/api/trades/page",
            params={"account_id": "A001", "limit": 501},
        )
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert page.json()["items"] == []
    assert too_large.status_code == 422
    assert trade_service.list_page.call_count == 1
    trade_service.get.assert_not_called()
