from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.api.websocket_ticket_api import get_websocket_ticket_service
from app.common.time_utils import utc_now
from app.core.security import get_current_user, get_token_service
from app.main import app
from app.realtime.websocket_ticket_service import IssuedWebSocketTicket


def test_websocket_ticket_requires_authentication():
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        response = TestClient(app).post("/api/ws/ticket")
    finally:
        app.dependency_overrides.update(previous)
    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"


def test_websocket_ticket_binds_verified_access_claims():
    user = SimpleNamespace(user_id="U001", role="USER")
    token_service = Mock()
    expires_at = utc_now() + timedelta(minutes=10)
    token_service.decode.return_value = SimpleNamespace(
        jti="ACCESS-JTI",
        expires_at=expires_at,
    )
    ticket_service = Mock()
    ticket_service.create.return_value = IssuedWebSocketTicket(
        ticket="one-time-ticket",
        expires_in=30,
    )
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_token_service] = lambda: token_service
    app.dependency_overrides[get_websocket_ticket_service] = (
        lambda: ticket_service
    )
    try:
        response = TestClient(app).post(
            "/api/ws/ticket",
            headers={"Authorization": "Bearer verified-access-token"},
        )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)

    assert response.status_code == 200
    assert response.json() == {
        "ticket": "one-time-ticket",
        "expires_in": 30,
    }
    ticket_service.create.assert_called_once_with(
        user_id="U001",
        role="USER",
        token_jti="ACCESS-JTI",
        token_expiration=expires_at,
    )
