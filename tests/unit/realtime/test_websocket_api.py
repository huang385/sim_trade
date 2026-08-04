from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from app.common.exceptions import AuthorizationError
from app.common.time_utils import utc_now
from app.realtime.connection_manager import ConnectionManager
from app.realtime.subscription_service import (
    RealtimeUserIdentity,
    SubscriptionAuthorization,
)
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
                return_value=SubscriptionAuthorization(
                    identity=RealtimeUserIdentity("U001", "USER"),
                    account_ids=frozenset({"A001"}),
                ),
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


@pytest.mark.parametrize(
    "failure",
    [
        RedisError("Redis snapshot unavailable"),
        AuthorizationError(
            "账户已经转移",
            error_code="WS_ACCOUNT_ACCESS_DENIED",
        ),
    ],
)
def test_snapshot_failure_or_second_authorization_change_sends_no_snapshot(
    failure,
):
    """快照屏障或二次授权失败时关闭连接，不推进客户端游标。"""

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
    authorization = SubscriptionAuthorization(
        identity=RealtimeUserIdentity("U001", "USER"),
        account_ids=frozenset({"A001"}),
    )

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "app.realtime.websocket_api._authenticate",
                return_value=(
                    claims,
                    authorization.identity,
                    authorization.account_ids,
                ),
            )
        )
        if isinstance(failure, RedisError):
            stack.enter_context(
                patch(
                    "app.realtime.websocket_api._authorize_subscription",
                    return_value=authorization,
                )
            )
            stack.enter_context(
                patch(
                    "app.realtime.websocket_api._build_snapshot",
                    side_effect=failure,
                )
            )
        else:
            stack.enter_context(
                patch(
                    "app.realtime.websocket_api._authorize_subscription",
                    side_effect=[authorization, failure],
                )
            )
            stack.enter_context(
                patch(
                    "app.realtime.websocket_api._build_snapshot",
                    return_value={"accounts": []},
                )
            )

        with TestClient(app).websocket_connect(
            "/ws/trading?ticket=single-use"
        ) as websocket:
            websocket.send_json(
                {"action": "subscribe", "account_ids": ["A001"]}
            )
            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_json()

    assert exc_info.value.code == 4452
    assert runtime.manager.active_count == 0
