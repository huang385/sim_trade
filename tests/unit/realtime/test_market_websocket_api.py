import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from app.realtime.market_websocket_api import (
    _event,
    _normalize_codes,
    _safe_source_status,
    router,
)
from app.realtime.connection_manager import ConnectionManager


def test_normalize_codes_is_batch_deduplicated_and_uppercase():
    assert _normalize_codes(
        {"order_book_ids": [" jd2609 ", "JD2609", "rb2610"]}
    ) == {"JD2609", "RB2610"}


@pytest.mark.parametrize(
    "message",
    [{}, {"order_book_ids": "JD2609"}, {"order_book_ids": []}],
)
def test_normalize_codes_rejects_invalid_protocol(message):
    with pytest.raises(ValueError):
        _normalize_codes(message)


def test_market_event_has_no_trading_stream_version_domain():
    result = json.loads(
        _event("MARKET_UPDATE", connection_id="C1", payload={"sequence_id": 7})
    )

    assert result["event_type"] == "MARKET_UPDATE"
    assert result["payload"]["sequence_id"] == 7
    assert "version" not in result
    assert "stream_id" not in result


def test_source_status_failure_is_sanitized_as_unavailable():
    class BrokenRedis:
        def hgetall(self, _key):
            raise RedisError("contains internal address")

    runtime = type("Runtime", (), {"redis_client": BrokenRedis()})()

    assert _safe_source_status(runtime) == {"status": "MARKET_UNAVAILABLE"}


def test_market_websocket_subscribe_snapshot_unsubscribe_and_cleanup(monkeypatch):
    class FakeRedis:
        def hgetall(self, _key):
            return {"status": "RUNNING", "api_token": "must-not-leak"}

        def xread(self, *_args, **_kwargs):
            time.sleep(0.01)
            return []

    class FakeClientSubscriptions:
        def __init__(self):
            self.requested = []
            self.removed_codes = []
            self.removed_connections = []

        def request_codes(self, *, connection_id, codes):
            self.requested.append((connection_id, set(codes)))
            return datetime(2026, 8, 11, 1, 3, tzinfo=timezone.utc)

        def remove_codes(self, *, connection_id, codes):
            self.removed_codes.append((connection_id, set(codes)))
            return len(codes)

        def remove_connection(self, connection_id):
            self.removed_connections.append(connection_id)
            return 1

    class FakeTicks:
        def get_latest_many(self, _keys):
            return {
                ("DCE", "JD2609"): {
                    "order_book_id": "JD2609",
                    "exchange_id": "DCE",
                    "symbol": "JD2609",
                    "sequence_id": "7",
                    "last_price": "3111.5",
                    "stream_message_id": "internal-only",
                }
            }

    subscriptions = FakeClientSubscriptions()
    runtime = SimpleNamespace(
        active=True,
        manager=ConnectionManager(max_connections_per_user=5),
        redis_client=FakeRedis(),
        client_market_subscription_store=subscriptions,
        market_tick_store=FakeTicks(),
        auth_service=SimpleNamespace(is_active=lambda *_args: True),
    )
    claims = SimpleNamespace(
        token_jti="jti",
        token_expiration=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    identity = SimpleNamespace(user_id="U1", role="USER")
    monkeypatch.setattr(
        "app.realtime.market_websocket_api._authenticate",
        lambda _runtime, _ticket: (claims, identity, frozenset()),
    )
    monkeypatch.setattr(
        "app.realtime.market_websocket_api._resolve_instruments",
        lambda codes: {
            code: {
                "order_book_id": code,
                "exchange_id": "DCE",
                "symbol": code,
            }
            for code in codes
        },
    )
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(router)

    with TestClient(app).websocket_connect("/ws/market?ticket=once") as socket:
        connected = socket.receive_json()
        assert connected["event_type"] == "MARKET_STATUS"
        assert connected["payload"]["source"] == {"status": "RUNNING"}

        socket.send_json({"action": "subscribe", "order_book_ids": ["jd2609"]})
        subscribed = socket.receive_json()
        snapshot = socket.receive_json()
        assert subscribed["event_type"] == "SUBSCRIPTION_STATUS"
        assert subscribed["payload"]["active_order_book_ids"] == ["JD2609"]
        assert snapshot["event_type"] == "MARKET_SNAPSHOT"
        assert snapshot["payload"]["sequence_id"] == "7"
        assert "stream_message_id" not in snapshot["payload"]

        socket.send_json({"action": "unsubscribe", "order_book_ids": ["JD2609"]})
        unsubscribed = socket.receive_json()
        assert unsubscribed["payload"]["active_order_book_ids"] == []
        socket.close()

    assert subscriptions.requested[0][1] == {"JD2609"}
    assert subscriptions.removed_codes[0][1] == {"JD2609"}
    assert len(subscriptions.removed_connections) == 1
