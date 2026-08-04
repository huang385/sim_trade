from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.common.exceptions import AuthenticationError
from app.common.time_utils import utc_now
from app.core.redis_client import redis_client
from app.core.database import SessionLocal, engine
from app.enums.auth_enums import UserRole, UserStatus
from app.infrastructure.redis_keys import (
    PNL_DIRTY_ACCOUNTS_KEY,
    PNL_DIRTY_ACCOUNT_VERSIONS_KEY,
    REALTIME_EVENT_STREAM,
    WS_GATEWAY_LEASE_KEY,
    pnl_account_key,
    projected_realtime_event_key,
)
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.models.account import Account
from app.models.app_user import AppUser
from app.realtime.gateway_app import app as gateway_app
from app.realtime.gateway_lease import GatewayLease
from app.realtime.event_enums import RealtimeEventType
from app.realtime.event_schema import RealtimeEventEnvelope
from app.realtime.event_store import RealtimeEventStore
from app.realtime.websocket_ticket_service import WebSocketTicketService
from app.realtime.snapshot_service import SnapshotService
from app.schemas.pnl_schema import AccountRealtimePnl
from fastapi.testclient import TestClient
from sqlalchemy import event, select


pytestmark = pytest.mark.integration


def _require_redis():
    try:
        redis_client.ping()
    except Exception as exc:
        pytest.skip(f"Redis不可连接: {exc}")


def test_real_redis_ticket_concurrent_consume_only_once():
    _require_redis()
    service = WebSocketTicketService(redis_client, expire_seconds=30)
    ticket = service.create(
        user_id=f"U-{uuid4().hex}",
        role="USER",
        token_jti=uuid4().hex,
        token_expiration=utc_now() + timedelta(minutes=5),
    ).ticket

    def consume():
        try:
            return service.consume(ticket).user_id
        except AuthenticationError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: consume(), range(8)))

    assert sum(result is not None for result in results) == 1


def test_real_redis_gateway_lease_allows_only_one_owner_and_safe_release():
    _require_redis()
    key = f"{WS_GATEWAY_LEASE_KEY}:test:{uuid4().hex}"
    lease = GatewayLease(redis_client, key=key, ttl_seconds=10)
    try:
        assert lease.acquire("owner-a") is True
        assert lease.acquire("owner-b") is False
        assert lease.release("owner-b") is False
        assert redis_client.get(key) == "owner-a"
        assert lease.renew("owner-a") is True
        assert lease.release("owner-a") is True
    finally:
        redis_client.delete(key)


def test_real_gateway_ticket_subscription_snapshot_and_denial():
    """使用真实PostgreSQL、Redis和Gateway生命周期验证完整连接主链路。"""

    _require_redis()
    try:
        with SessionLocal() as db:
            row = db.execute(
                select(AppUser, Account)
                .join(Account, Account.user_id == AppUser.user_id)
                .where(AppUser.status == UserStatus.ACTIVE.value)
                .order_by(AppUser.id, Account.id)
                .limit(1)
            ).first()
            if row is None:
                admin = db.scalar(
                    select(AppUser)
                    .where(
                        AppUser.status == UserStatus.ACTIVE.value,
                        AppUser.role == UserRole.ADMIN.value,
                    )
                    .limit(1)
                )
                account = db.scalar(select(Account).limit(1))
                if admin is None or account is None:
                    pytest.skip("缺少可用于Gateway集成测试的活动用户或账户")
                user = admin
            else:
                user, account = row
            user_id = user.user_id
            role = user.role
            account_id = account.account_id
    except Exception as exc:
        pytest.skip(f"PostgreSQL不可连接或结构未迁移: {exc}")

    issued = WebSocketTicketService(redis_client).create(
        user_id=user_id,
        role=role,
        token_jti=uuid4().hex,
        token_expiration=utc_now() + timedelta(minutes=5),
    )
    try:
        with TestClient(gateway_app) as client:
            with client.websocket_connect(
                f"/ws/trading?ticket={issued.ticket}"
            ) as websocket:
                websocket.send_json(
                    {
                        "action": "subscribe",
                        "account_ids": [account_id],
                    }
                )
                snapshot = websocket.receive_json()
                assert snapshot["event_type"] == "SNAPSHOT"
                assert snapshot["payload"]["account_ids"] == [account_id]
                assert snapshot["payload"]["accounts"][0]["account"][
                    "account_id"
                ] == account_id

                websocket.send_json(
                    {
                        "action": "subscribe",
                        "account_ids": [f"MISSING-{uuid4().hex}"],
                    }
                )
                denied = websocket.receive_json()
                assert denied["event_type"] == "ERROR"
                assert denied["payload"]["error_code"] == (
                    "WS_ACCOUNT_ACCESS_DENIED"
                )
    except RuntimeError as exc:
        if "单实例租约" in str(exc):
            pytest.skip(f"已有Gateway正在运行: {exc}")
        raise


def test_real_redis_projection_is_idempotent_and_pnl_event_is_atomic():
    """真实Lua验证投影去重及账户PnL快照与事件同批写入。"""

    _require_redis()
    suffix = uuid4().hex
    source_event_id = f"WS-PROJECTION-{suffix}"
    account_id = f"WS-A-{suffix}"
    event_store = RealtimeEventStore(redis_client)
    projected = RealtimeEventEnvelope(
        event_id=source_event_id,
        event_type=RealtimeEventType.ORDER_CREATED,
        account_id=account_id,
        entity_id=f"O-{suffix}",
        occurred_at=utc_now(),
        version="1-0",
        payload={"frozen_margin": "123.450000"},
    )
    message_ids: list[str] = []
    try:
        first = event_store.publish_projected_once(projected)
        second = event_store.publish_projected_once(projected)
        assert first is not None
        assert second is None
        message_ids.append(first)

        account_model = AccountRealtimePnl(
            account_id=account_id,
            cumulative_unrealized_pnl=Decimal("12.340000"),
            daily_position_pnl=Decimal("12.340000"),
            daily_close_pnl=Decimal("1.000000"),
            daily_commission=Decimal("0.500000"),
            daily_pnl=Decimal("12.840000"),
            equity=Decimal("100012.340000"),
            available_cash=Decimal("99888.890000"),
            risk_ratio=Decimal("0.00123456"),
            updated_at=utc_now(),
        )
        before = event_store.current_cursor()
        RealtimePnlStore(redis_client).write_cycle_snapshots(
            positions=[],
            accounts=[account_model],
            dirty_version=f"cycle-{suffix}",
            active_positions=[],
            closed_positions=[],
        )
        rows = redis_client.xrange(
            REALTIME_EVENT_STREAM,
            # Redis 5不支持XRANGE的"(id"排他语法，包含读取后手工排除游标。
            min=before,
            max="+",
        )
        own_rows = [
            (message_id, fields)
            for message_id, fields in rows
            if message_id != before
            and fields.get("account_id") == account_id
        ]
        message_ids.extend(message_id for message_id, _fields in own_rows)
        assert {fields["event_type"] for _id, fields in own_rows} == {
            "ACCOUNT_UPDATED",
            "RISK_STATE_CHANGED",
        }
        assert redis_client.hget(
            pnl_account_key(account_id),
            "cumulative_unrealized_pnl",
        ) == "12.340000"
    finally:
        if message_ids:
            redis_client.xdel(REALTIME_EVENT_STREAM, *message_ids)
        redis_client.delete(projected_realtime_event_key(source_event_id))
        redis_client.delete(pnl_account_key(account_id))
        redis_client.srem(PNL_DIRTY_ACCOUNTS_KEY, account_id)
        redis_client.hdel(PNL_DIRTY_ACCOUNT_VERSIONS_KEY, account_id)


def test_real_snapshot_uses_fixed_query_count_without_position_n_plus_one():
    """持仓数量增加不会把完整快照SQL放大为逐持仓查询。"""

    _require_redis()
    try:
        with SessionLocal() as db:
            account_id = db.scalar(
                select(Account.account_id).order_by(Account.id).limit(1)
            )
        if account_id is None:
            pytest.skip("数据库中没有可用于快照测试的账户")
    except Exception as exc:
        pytest.skip(f"PostgreSQL不可连接或结构未迁移: {exc}")

    statements: list[str] = []

    def count_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_sql)
    try:
        with SessionLocal() as db:
            result = SnapshotService(RealtimePnlStore(redis_client)).build(
                db,
                {account_id},
            )
    finally:
        event.remove(engine, "before_cursor_execute", count_sql)

    assert result["accounts"][0]["account"]["account_id"] == account_id
    # 账户、持仓、活动订单、当日成交固定4次；存在持仓时再批量读取一次明细。
    assert 4 <= len(statements) <= 5
