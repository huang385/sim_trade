from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.common.exceptions import AuthenticationError
from app.common.time_utils import utc_now
from app.core.database import SessionLocal, engine
from app.core.redis_client import redis_client
from app.infrastructure.redis_keys import (
    PNL_DIRTY_ACCOUNTS_KEY,
    PNL_DIRTY_ACCOUNT_VERSIONS_KEY,
    PNL_DIRTY_POSITIONS_KEY,
    PNL_DIRTY_POSITION_VERSIONS_KEY,
    REALTIME_EVENT_STREAM,
    WS_GATEWAY_LEASE_KEY,
    pnl_account_key,
    pnl_position_key,
    projected_realtime_event_key,
    realtime_aggregate_business_version_key,
)
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.infrastructure.order_stream_consumer import OrderStreamConsumer
from app.infrastructure.order_event_publisher import OrderEventPublisher
from app.models.account import Account
from app.models.outbox_event import OutboxEvent
from app.models.position import Position
from app.models.trade import Trade
from app.matching.models import MatchResult
from app.realtime.event_consumer import RealtimeEventConsumer
from app.realtime.gateway_lease import GatewayLease
from app.realtime.gateway_runtime import GatewayRuntime
from app.realtime.snapshot_service import SnapshotService
from app.realtime.websocket_api import router as websocket_router
from app.realtime.event_enums import RealtimeEventType
from app.realtime.event_schema import RealtimeEventEnvelope
from app.realtime.event_store import RealtimeEventStore
from app.realtime.event_router import RealtimeEventRouter
from app.realtime.websocket_ticket_service import WebSocketTicketService
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.pnl_schema import AccountRealtimePnl, PositionRealtimePnl
from app.services.trade_settlement_service import (
    SettlementCommand,
    TradeSettlementService,
)
from app.workers.outbox_publisher_worker import OutboxPublisherWorker
from app.workers.realtime_event_projection_worker import (
    RealtimeEventProjectionWorker,
)
from tests.integration.conftest import make_order_service, make_request


pytestmark = pytest.mark.integration


class _ReservedOutboxRepository(OutboxRepository):
    """让端到端测试事件不被本机正在运行的正式发布Worker抢占。"""

    @staticmethod
    def create_event(*args, **kwargs):
        event = OutboxRepository.create_event(*args, **kwargs)
        event.status = "PROCESSING"
        event.next_retry_at = utc_now() + timedelta(minutes=5)
        return event


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


def test_real_redis_old_gateway_cannot_ack_after_lease_handover():
    _require_redis()
    suffix = uuid4().hex
    key = f"{WS_GATEWAY_LEASE_KEY}:fence:{suffix}"
    stream = f"stream:ws-fence:{suffix}"
    group = f"group:ws-fence:{suffix}"
    lease = GatewayLease(redis_client, key=key, ttl_seconds=10)
    try:
        message_id = redis_client.xadd(stream, {"payload": "{}"})
        redis_client.xgroup_create(stream, group, id="0-0")
        redis_client.xreadgroup(group, "old", {stream: ">"}, count=1)
        assert lease.acquire("old") is True
        assert lease.release("old") is True
        assert lease.acquire("new") is True

        owned, acknowledged = lease.acknowledge_if_owned(
            owner_id="old",
            stream_name=stream,
            group_name=group,
            message_ids=[message_id],
        )
        assert (owned, acknowledged) == (False, 0)
        assert redis_client.xpending(stream, group)["pending"] == 1

        owned, acknowledged = lease.acknowledge_if_owned(
            owner_id="new",
            stream_name=stream,
            group_name=group,
            message_ids=[message_id],
        )
        assert (owned, acknowledged) == (True, 1)
    finally:
        redis_client.delete(key, stream)


def test_real_redis_new_gateway_group_skips_existing_history():
    _require_redis()
    suffix = uuid4().hex
    stream = f"stream:ws-start:{suffix}"
    group = f"group:ws-start:{suffix}"
    try:
        old_id = redis_client.xadd(stream, {"event_id": "old"})
        consumer = OrderStreamConsumer(
            redis_client,
            stream_name=stream,
            group_name=group,
            consumer_name="gateway",
            dead_letter_stream=f"{stream}:dead",
            failure_ttl_seconds=60,
            group_start_id="$",
        )
        consumer.ensure_group()
        new_id = redis_client.xadd(stream, {"event_id": "new"})

        rows = consumer.read_new_messages(batch_size=10, block_ms=1)

        assert [message_id for message_id, _fields in rows] == [new_id]
        assert old_id != new_id
    finally:
        redis_client.delete(stream, f"{stream}:dead")


def test_real_gateway_ticket_subscription_snapshot_and_denial(
    integration_context,
):
    """使用真实PostgreSQL、Redis和Gateway生命周期验证完整连接主链路。"""

    _require_redis()
    user_id = integration_context.user_id
    account_id = integration_context.account_id

    issued = WebSocketTicketService(redis_client).create(
        user_id=user_id,
        role="USER",
        token_jti=uuid4().hex,
        token_expiration=utc_now() + timedelta(minutes=5),
    )
    # 使用隔离Runtime测试真实WebSocket协议，不启动事件消费任务，也不抢占
    # 正在运行的全局Gateway租约或消费其生产Consumer Group。
    isolated_app = FastAPI()
    isolated_runtime = GatewayRuntime()
    isolated_runtime.active = True
    isolated_app.state.runtime = isolated_runtime
    isolated_app.include_router(websocket_router)
    with TestClient(isolated_app) as client:
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


def test_real_order_outbox_projection_reaches_websocket(integration_context):
    """真实验证订单事务经两个Stream和Gateway到达WebSocket客户端。"""

    _require_redis()
    suffix = uuid4().hex
    order_stream = f"stream:ws-e2e-orders:{suffix}"
    realtime_stream = f"stream:ws-e2e-realtime:{suffix}"
    projection_group = f"group:ws-e2e-projection:{suffix}"
    gateway_group = f"group:ws-e2e-gateway:{suffix}"
    lease_key = f"ws:gateway:lease:e2e:{suffix}"
    dead_streams = [
        f"{order_stream}:dead",
        f"{realtime_stream}:dead",
    ]

    runtime = GatewayRuntime()
    runtime.event_store = RealtimeEventStore(
        redis_client,
        stream_name=realtime_stream,
    )
    runtime.lease = GatewayLease(
        redis_client,
        key=lease_key,
        ttl_seconds=30,
    )
    gateway_stream_consumer = OrderStreamConsumer(
        redis_client,
        stream_name=realtime_stream,
        group_name=gateway_group,
        consumer_name=f"gateway-{suffix}",
        dead_letter_stream=dead_streams[1],
        failure_ttl_seconds=60,
        group_start_id="$",
    )
    runtime.consumer = RealtimeEventConsumer(
        consumer=gateway_stream_consumer,
        router=RealtimeEventRouter(runtime.manager),
        lease=runtime.lease,
        owner_id=runtime.owner_id,
        on_lease_lost=runtime._handle_lease_lost,
    )

    projection_consumer = OrderStreamConsumer(
        redis_client,
        stream_name=order_stream,
        group_name=projection_group,
        consumer_name=f"projection-{suffix}",
        dead_letter_stream=dead_streams[0],
        failure_ttl_seconds=60,
        group_start_id="0-0",
    )
    projection_consumer.ensure_group()
    projection_worker = RealtimeEventProjectionWorker(
        consumer=projection_consumer,
        event_store=runtime.event_store,
        batch_size=10,
        block_ms=1,
        pending_idle_ms=0,
    )

    @asynccontextmanager
    async def lifespan(_app):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    isolated_app = FastAPI(lifespan=lifespan)
    isolated_app.state.runtime = runtime
    isolated_app.include_router(websocket_router)
    issued = WebSocketTicketService(redis_client).create(
        user_id=integration_context.user_id,
        role="USER",
        token_jti=uuid4().hex,
        token_expiration=utc_now() + timedelta(minutes=5),
    )
    outbox_repository = _ReservedOutboxRepository()
    projected_event_ids: list[str] = []
    projected_aggregates: set[tuple[str, str]] = set()
    order_id = None
    position_id = None
    try:
        with TestClient(isolated_app) as client:
            with client.websocket_connect(
                f"/ws/trading?ticket={issued.ticket}"
            ) as websocket:
                websocket.send_json(
                    {
                        "action": "subscribe",
                        "account_ids": [integration_context.account_id],
                    }
                )
                assert websocket.receive_json()["event_type"] == "SNAPSHOT"

                with SessionLocal() as db:
                    order = make_order_service(
                        integration_context,
                        outbox_repository=outbox_repository,
                    ).create_order(
                        db,
                        make_request(
                            integration_context,
                            client_order_id=f"WS-E2E-{suffix}",
                        ),
                    )
                    order_id = order.order_id

                with SessionLocal() as db:
                    outbox = db.scalar(
                        select(OutboxEvent).where(
                            OutboxEvent.aggregate_type == "ORDER",
                            OutboxEvent.aggregate_id == order_id,
                            OutboxEvent.event_type == "ORDER_ACCEPTED",
                        )
                    )
                    assert outbox is not None
                    accepted_event_id = outbox.event_id

                publisher_worker = OutboxPublisherWorker(
                    session_factory=SessionLocal,
                    publisher=OrderEventPublisher(
                        redis_client,
                        stream_name=order_stream,
                    ),
                )
                assert publisher_worker._publish_one(accepted_event_id) == "sent"
                projected_event_ids.append(accepted_event_id)
                projected_aggregates.add(("ORDER", order_id))
                assert projection_worker.run_once() == 1

                event_payload = websocket.receive_json()
                assert event_payload["event_type"] == "ORDER_CREATED"
                assert event_payload["payload"]["order_id"] == order_id
                assert event_payload["payload"]["status"] == "ACCEPTED"
                assert event_payload["business_version"].isdigit()

                # 使用真实结算服务完成整单成交；账户、订单、Trade、Position
                # 和四类Outbox事实仍处于同一个PostgreSQL事务。
                with SessionLocal() as db:
                    settlement = TradeSettlementService(
                        outbox_repository=outbox_repository,
                    ).settle(
                        db,
                        SettlementCommand(
                            order_id=order_id,
                            market_event_id=f"TICK-WS-E2E-{suffix}",
                            market_stream_message_id=f"{suffix}-0",
                            tick_event_time=utc_now(),
                            tick_sequence_id=1,
                            match_result=MatchResult(
                                matched=True,
                                fill_price=Decimal("3499"),
                                fill_volume=2,
                                reason=None,
                                engine_name="VN",
                                engine_version="1.0",
                            ),
                        ),
                    )
                    assert settlement.action == "SETTLED"

                with SessionLocal() as db:
                    trade = db.scalar(
                        select(Trade).where(Trade.order_id == order_id)
                    )
                    position = db.scalar(
                        select(Position).where(
                            Position.account_id
                            == integration_context.account_id
                        )
                    )
                    account = db.scalar(
                        select(Account).where(
                            Account.account_id
                            == integration_context.account_id
                        )
                    )
                    assert trade is not None and position is not None
                    position_id = position.position_id
                    aggregate_ids = {
                        order_id,
                        integration_context.account_id,
                        trade.trade_id,
                        position.position_id,
                    }
                    pending_events = db.scalars(
                        select(OutboxEvent)
                        .where(
                            OutboxEvent.aggregate_id.in_(aggregate_ids),
                            OutboxEvent.status == "PROCESSING",
                        )
                        .order_by(OutboxEvent.id)
                    ).all()
                    assert len(pending_events) == 5
                    account_expected = format(account.cash_balance, "f")
                    position_expected = position.total_volume

                for event in pending_events:
                    assert publisher_worker._publish_one(event.event_id) == "sent"
                    projected_event_ids.append(event.event_id)
                    projected_aggregates.add(
                        (event.aggregate_type, event.aggregate_id)
                    )
                assert projection_worker.run_once() == len(pending_events)

                settlement_events = [
                    websocket.receive_json() for _event in pending_events
                ]
                event_types = [item["event_type"] for item in settlement_events]
                assert "TRADE_CREATED" in event_types
                assert "ORDER_UPDATED" in event_types
                assert "POSITION_UPDATED" in event_types
                assert event_types.count("ACCOUNT_UPDATED") == 2
                position_event = next(
                    item
                    for item in settlement_events
                    if item["event_type"] == "POSITION_UPDATED"
                )
                account_event = settlement_events[-1]
                assert position_event["payload"]["total_volume"] == (
                    position_expected
                )
                assert account_event["event_type"] == "ACCOUNT_UPDATED"
                assert account_event["payload"]["cash_balance"] == (
                    account_expected
                )

                # 写入真实Redis PnL Hash后，把同一绝对快照写入本测试隔离的
                # 实时Stream，验证Gateway和客户端的PNL_UPDATED应用链路。
                # 生产全局Stream的Hash+事件原子性由下方独立真实Lua测试覆盖。
                now = utc_now()
                position_pnl = PositionRealtimePnl(
                    position_id=position.position_id,
                    account_id=position.account_id,
                    exchange_id=position.exchange_id,
                    symbol=position.symbol,
                    direction=position.direction,
                    mark_price=Decimal("3501"),
                    cumulative_unrealized_pnl=Decimal("40"),
                    daily_position_pnl=Decimal("40"),
                    event_time=now,
                    source_event_id=f"PNL-TICK-{suffix}",
                    updated_at=now,
                )
                account_pnl = AccountRealtimePnl(
                    account_id=account.account_id,
                    cumulative_unrealized_pnl=Decimal("40"),
                    daily_position_pnl=Decimal("40"),
                    daily_close_pnl=account.daily_close_pnl,
                    daily_commission=account.daily_commission,
                    daily_pnl=Decimal("34"),
                    equity=account.cash_balance + Decimal("40"),
                    available_cash=account.available_cash + Decimal("40"),
                    risk_ratio=account.risk_ratio,
                    updated_at=now,
                )
                RealtimePnlStore(redis_client).write_snapshots(
                    positions=[position_pnl],
                    accounts=[account_pnl],
                    dirty_version=f"ws-e2e-{suffix}",
                )
                runtime.event_store.publish(
                    RealtimeEventEnvelope(
                        event_id=f"PNL-WS-E2E-{suffix}",
                        event_type=RealtimeEventType.PNL_UPDATED,
                        account_id=position.account_id,
                        entity_id=position.position_id,
                        occurred_at=now,
                        version="0-0",
                        payload={
                            key: str(value)
                            for key, value in position_pnl.model_dump().items()
                        },
                    )
                )
                pnl_event = websocket.receive_json()
                assert pnl_event["event_type"] == "PNL_UPDATED"
                assert pnl_event["payload"]["cumulative_unrealized_pnl"] == (
                    "40"
                )
                assert redis_client.hget(
                    pnl_position_key(position.position_id),
                    "cumulative_unrealized_pnl",
                ) == "40"
    finally:
        redis_client.delete(
            order_stream,
            realtime_stream,
            lease_key,
            *dead_streams,
        )
        if projected_event_ids:
            redis_client.delete(
                *(
                    projected_realtime_event_key(event_id)
                    for event_id in projected_event_ids
                )
            )
        if projected_aggregates:
            redis_client.delete(
                *(
                    realtime_aggregate_business_version_key(
                        aggregate_type,
                        aggregate_id,
                    )
                    for aggregate_type, aggregate_id in projected_aggregates
                )
            )
        redis_client.delete(
            pnl_account_key(integration_context.account_id),
            *(
                [pnl_position_key(position_id)]
                if position_id is not None
                else []
            ),
        )
        redis_client.srem(
            PNL_DIRTY_ACCOUNTS_KEY,
            integration_context.account_id,
        )
        redis_client.hdel(
            PNL_DIRTY_ACCOUNT_VERSIONS_KEY,
            integration_context.account_id,
        )
        if position_id is not None:
            redis_client.srem(PNL_DIRTY_POSITIONS_KEY, position_id)
            redis_client.hdel(PNL_DIRTY_POSITION_VERSIONS_KEY, position_id)


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
        business_version="2",
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
        redis_client.delete(
            realtime_aggregate_business_version_key(
                "ORDER",
                projected.entity_id,
            )
        )
        redis_client.delete(pnl_account_key(account_id))
        redis_client.srem(PNL_DIRTY_ACCOUNTS_KEY, account_id)
        redis_client.hdel(PNL_DIRTY_ACCOUNT_VERSIONS_KEY, account_id)


@pytest.mark.parametrize(
    (
        "new_event_type",
        "new_status",
        "old_event_type",
        "old_status",
    ),
    [
        (
            RealtimeEventType.ORDER_UPDATED,
            "FILLED",
            RealtimeEventType.ORDER_CREATED,
            "ACCEPTED",
        ),
        (
            RealtimeEventType.ORDER_UPDATED,
            "PARTIALLY_FILLED",
            RealtimeEventType.ORDER_CREATED,
            "ACCEPTED",
        ),
        (
            RealtimeEventType.ORDER_CANCELLED,
            "CANCELLED",
            RealtimeEventType.ORDER_CREATED,
            "ACCEPTED",
        ),
        (
            RealtimeEventType.ORDER_UPDATED,
            "FILLED",
            RealtimeEventType.ORDER_UPDATED,
            "PARTIALLY_FILLED",
        ),
        (
            RealtimeEventType.ORDER_CANCELLED,
            "PARTIALLY_CANCELLED",
            RealtimeEventType.ORDER_UPDATED,
            "PARTIALLY_FILLED",
        ),
    ],
)
def test_real_redis_late_order_event_cannot_roll_state_back(
    new_event_type,
    new_status,
    old_event_type,
    old_status,
):
    """真实Lua验证迟到旧状态不能覆盖已投影的较新订单事实。"""

    _require_redis()
    suffix = uuid4().hex
    order_id = f"O-{suffix}"
    account_id = f"A-{suffix}"
    store = RealtimeEventStore(redis_client)
    newer = RealtimeEventEnvelope(
        event_id=f"E-NEW-{suffix}",
        event_type=new_event_type,
        account_id=account_id,
        entity_id=order_id,
        occurred_at=utc_now(),
        version="source-new",
        business_version="20",
        payload={"order_id": order_id, "status": new_status},
    )
    older = RealtimeEventEnvelope(
        event_id=f"E-OLD-{suffix}",
        event_type=old_event_type,
        account_id=account_id,
        entity_id=order_id,
        occurred_at=utc_now(),
        version="source-late",
        business_version="10",
        payload={"order_id": order_id, "status": old_status},
    )
    message_id = None
    try:
        message_id = store.publish_projected_once(newer)
        assert message_id is not None
        assert store.publish_projected_once(older) is None
        rows = redis_client.xrange(
            REALTIME_EVENT_STREAM,
            min=message_id,
            max=message_id,
        )
        assert len(rows) == 1
        assert rows[0][1]["event_type"] == new_event_type.value
    finally:
        if message_id:
            redis_client.xdel(REALTIME_EVENT_STREAM, message_id)
        redis_client.delete(
            projected_realtime_event_key(newer.event_id),
            projected_realtime_event_key(older.event_id),
            realtime_aggregate_business_version_key("ORDER", order_id),
        )


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
