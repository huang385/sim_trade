import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/api/auth/me", {}),
        ("get", "/api/accounts", {}),
        ("post", "/api/accounts", {"json": {}}),
        (
            "get",
            "/api/orders",
            {"params": {"account_id": "A001"}},
        ),
        (
            "post",
            "/api/orders",
            {"json": {}},
        ),
        (
            "post",
            "/api/market-data/subscriptions/prepare",
            {"json": {}},
        ),
        (
            "get",
            "/api/market-data/subscriptions/status",
            {
                "params": {
                    "account_id": "A001",
                    "exchange_id": "DCE",
                    "symbol": "JD2609-C-4000",
                }
            },
        ),
        (
            "get",
            "/api/trades",
            {"params": {"account_id": "A001"}},
        ),
        (
            "get",
            "/api/positions",
            {"params": {"account_id": "A001"}},
        ),
        ("get", "/api/accounts/A001/pnl/realtime", {}),
        ("get", "/api/accounts/A001/trading-snapshot", {}),
        ("put", "/api/admin/instruments", {"json": {}}),
        ("put", "/api/admin/margin-rules/current", {"json": {}}),
        ("put", "/api/admin/fee-rules/current", {"json": {}}),
        ("get", "/api/admin/users", {}),
    ],
)
def test_business_and_admin_apis_require_authentication(
    method, path, kwargs
):
    response = getattr(TestClient(app), method)(path, **kwargs)

    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"
