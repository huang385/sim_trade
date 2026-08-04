import asyncio
from contextlib import ExitStack
from datetime import timedelta
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
from app.realtime.connection_context import ConnectionContext
from app.realtime.subscription_service import (
    RealtimeUserIdentity,
    SubscriptionAuthorization,
)
from app.realtime.websocket_api import _connection_monitor, router


class _BlockingWebSocket:
    """模拟发送被阻塞的慢客户端，用于验证权限撤销后的失败关闭。"""

    def __init__(self):
        self.send_started = asyncio.Event()
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None

    async def send_text(self, value: str) -> None:
        self.send_started.set()
        await asyncio.Future()
        self.sent.append(value)

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)


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


def test_periodic_permission_recheck_closes_connection_and_drops_queue(
    monkeypatch,
):
    """账户转移或管理员降级后，旧连接不得继续输出已经积压的数据。"""

    async def scenario():
        manager = ConnectionManager()
        websocket = _BlockingWebSocket()
        context = ConnectionContext(
            connection_id="C-REVOKED",
            websocket=websocket,
            user_id="U001",
            role="ADMIN",
            token_jti="JTI",
            token_expiration=utc_now() + timedelta(hours=1),
            connected_at=utc_now(),
            send_queue=asyncio.Queue(maxsize=5),
            authorized_account_ids=frozenset({"A001", "B001"}),
        )
        assert await manager.register(context) is True
        manager.subscribe(
            context,
            {"A001", "B001"},
            snapshot_loading=True,
        )
        context.snapshot_buffers["A001"].append(("9-0", "BUFFERED"))

        # 第一条敏感消息已被sender取走但发送阻塞，第二条仍位于队列中。
        context.send_queue.put_nowait("SECRET-IN-FLIGHT")
        await websocket.send_started.wait()
        context.send_queue.put_nowait("SECRET-QUEUED")

        authorization = SubscriptionAuthorization(
            identity=RealtimeUserIdentity("U001", "USER"),
            account_ids=frozenset({"A001"}),
        )
        monkeypatch.setattr(
            "app.realtime.websocket_api._recheck_subscriptions",
            lambda _runtime, _context: authorization,
        )
        monkeypatch.setattr(
            "app.realtime.websocket_api.settings."
            "ws_auth_recheck_interval_seconds",
            0.01,
        )
        monkeypatch.setattr(
            "app.realtime.websocket_api.settings."
            "ws_heartbeat_interval_seconds",
            60,
        )
        runtime = SimpleNamespace(manager=manager)

        await asyncio.wait_for(
            _connection_monitor(runtime, context),
            timeout=1,
        )

        assert websocket.sent == []
        assert websocket.closed is not None
        assert websocket.closed[0] == 4403
        assert context.sender_task.cancelled()
        assert context.send_queue.empty()
        assert context.authorized_account_ids == frozenset()
        assert context.subscribed_account_ids == set()
        assert context.snapshot_buffers == {}
        assert context.snapshot_loading_accounts == set()
        assert context.last_versions == {}
        assert manager.active_count == 0
        assert manager.connections_by_user == {}
        assert manager.connections_by_account == {}

    asyncio.run(scenario())
