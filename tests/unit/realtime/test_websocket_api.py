from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.common.time_utils import utc_now
from app.realtime.connection_manager import ConnectionManager
from app.realtime.subscription_service import RealtimeUserIdentity
from app.realtime.websocket_api import router


def test_websocket_subscribe_receives_snapshot_first():
    app = FastAPI()
    runtime = SimpleNamespace(
        active=True,
        manager=ConnectionManager(),
        event_store=Mock(current_cursor=Mock(return_value="100-0")),
    )
    app.state.runtime = runtime
    app.include_router(router)
    claims = SimpleNamespace(
        token_jti="JTI",
        token_expiration=utc_now().replace(year=2099),
    )

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "app.realtime.websocket_api._authenticate",
                return_value=(
                    claims,
                    RealtimeUserIdentity("U001", "USER"),
                    frozenset({"A001"}),
                ),
            )
        )
        stack.enter_context(
            patch(
                "app.realtime.websocket_api._authorize_subscription",
                return_value=frozenset({"A001"}),
            )
        )
        stack.enter_context(
            patch(
                "app.realtime.websocket_api._build_snapshot",
                return_value={
                    "generated_at": utc_now().isoformat(),
                    "accounts": [
                        {"account": {"account_id": "A001"}}
                    ],
                },
            )
        )
        with TestClient(app).websocket_connect(
            "/ws/trading?ticket=single-use"
        ) as websocket:
            websocket.send_json(
                {"action": "subscribe", "account_ids": ["A001"]}
            )
            event = websocket.receive_json()

    assert event["event_type"] == "SNAPSHOT"
    assert event["version"] == "100-0"
    assert event["payload"]["account_ids"] == ["A001"]


def test_websocket_rejects_when_gateway_lost_single_instance_lease():
    app = FastAPI()
    app.state.runtime = SimpleNamespace(active=False)
    app.include_router(router)

    with TestClient(app) as client:
        try:
            with client.websocket_connect(
                "/ws/trading?ticket=unused"
            ) as websocket:
                websocket.receive_json()
        except Exception as exc:
            assert "4503" in str(exc) or getattr(exc, "code", None) == 4503
