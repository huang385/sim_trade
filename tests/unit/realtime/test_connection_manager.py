import asyncio
from datetime import timedelta

from app.common.time_utils import utc_now
from app.realtime.connection_context import ConnectionContext
from app.realtime.connection_manager import ConnectionManager
from app.realtime.event_enums import RealtimeEventType
from app.realtime.event_schema import RealtimeEventEnvelope


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = None

    async def send_text(self, value):
        self.sent.append(value)

    async def close(self, *, code, reason):
        self.closed = (code, reason)


class SlowWebSocket(FakeWebSocket):
    """send_text持续阻塞，模拟网络背压导致的慢客户端。"""

    def __init__(self):
        super().__init__()
        self.send_started = asyncio.Event()

    async def send_text(self, value):
        self.send_started.set()
        await asyncio.Future()
        self.sent.append(value)


def make_context(*, queue_size=10):
    return ConnectionContext(
        connection_id="C001",
        websocket=FakeWebSocket(),
        user_id="U001",
        role="USER",
        token_jti="JTI",
        token_expiration=utc_now() + timedelta(minutes=5),
        connected_at=utc_now(),
        send_queue=asyncio.Queue(maxsize=queue_size),
    )


def test_snapshot_is_sent_before_buffered_newer_event():
    async def scenario():
        manager = ConnectionManager()
        context = make_context()
        manager.connections_by_id[context.connection_id] = context
        manager.subscribe(context, {"A001"}, snapshot_loading=True)
        event = RealtimeEventEnvelope(
            event_id="E1",
            event_type=RealtimeEventType.ORDER_UPDATED,
            account_id="A001",
            entity_id="O1",
            occurred_at=utc_now(),
            version="11-0",
            payload={"status": "FILLED"},
        )
        await manager.broadcast_to_account(event, event.model_dump_json())

        await manager.finish_snapshot(
            context,
            account_ids={"A001"},
            cursor="10-0",
            snapshot_serialized="SNAPSHOT",
        )

        assert await context.send_queue.get() == "SNAPSHOT"
        assert "ORDER_UPDATED" in await context.send_queue.get()
        assert context.last_versions["A001"] == "11-0"

    asyncio.run(scenario())


def test_old_or_duplicate_event_is_not_sent_to_account():
    async def scenario():
        manager = ConnectionManager()
        context = make_context()
        manager.connections_by_id[context.connection_id] = context
        manager.subscribe(context, {"A001"}, snapshot_loading=False)
        context.last_versions["A001"] = "20-0"
        event = RealtimeEventEnvelope(
            event_id="E1",
            event_type=RealtimeEventType.ACCOUNT_PNL_UPDATED,
            account_id="A001",
            entity_id="A001",
            occurred_at=utc_now(),
            version="19-0",
            payload={},
        )

        assert await manager.broadcast_to_account(event, "OLD") == 0
        assert context.send_queue.empty()

    asyncio.run(scenario())


def test_full_queue_closes_slow_connection_without_blocking():
    async def scenario():
        manager = ConnectionManager()
        context = make_context(queue_size=1)
        manager.connections_by_id[context.connection_id] = context
        manager.connections_by_user[context.user_id].add(
            context.connection_id
        )
        context.send_queue.put_nowait("FULL")

        assert await manager.enqueue(context, "NEXT") is False
        assert context.websocket.closed[0] == 4451
        assert context.connection_id not in manager.connections_by_id

    asyncio.run(scenario())


def test_one_serialized_event_routes_to_one_hundred_connections():
    """轻量压力测试：路由不查库、不重复序列化且队列保持有界。"""

    async def scenario():
        manager = ConnectionManager(max_connections_per_user=200)
        contexts = []
        for index in range(100):
            context = make_context(queue_size=2)
            context.connection_id = f"C-{index}"
            manager.connections_by_id[context.connection_id] = context
            manager.subscribe(context, {"A001"}, snapshot_loading=False)
            contexts.append(context)
        event = RealtimeEventEnvelope(
            event_id="E-PRESSURE",
            event_type=RealtimeEventType.ACCOUNT_PNL_UPDATED,
            account_id="A001",
            entity_id="A001",
            occurred_at=utc_now(),
            version="200-0",
            payload={"equity": "100000.000000"},
        )
        serialized = event.model_dump_json()

        assert await manager.broadcast_to_account(event, serialized) == 100
        assert all(context.send_queue.qsize() == 1 for context in contexts)
        delivered = [
            await context.send_queue.get() for context in contexts
        ]
        assert delivered == [serialized] * 100

    asyncio.run(scenario())


def test_permission_revocation_close_cancels_sender_and_discards_all_state():
    """队列已积压敏感事件时，失败关闭不会继续向旧用户输出。"""

    async def scenario():
        manager = ConnectionManager()
        context = make_context(queue_size=5)
        context.websocket = SlowWebSocket()
        context.authorized_account_ids = frozenset({"A001", "B001"})
        assert await manager.register(context) is True
        manager.subscribe(context, {"A001", "B001"}, snapshot_loading=True)
        context.snapshot_buffers["A001"].append(("10-0", "BUFFERED"))

        context.send_queue.put_nowait("SECRET-IN-FLIGHT")
        await context.websocket.send_started.wait()
        context.send_queue.put_nowait("SECRET-QUEUED")

        await manager.close(
            context,
            code=4403,
            reason="账户订阅权限已撤销",
        )

        assert context.websocket.sent == []
        assert context.websocket.closed[0] == 4403
        assert context.sender_task.cancelled()
        assert context.send_queue.empty()
        assert context.subscribed_account_ids == set()
        assert context.authorized_account_ids == frozenset()
        assert context.snapshot_buffers == {}
        assert context.snapshot_loading_accounts == set()
        assert context.last_versions == {}
        assert context.connection_id not in manager.connections_by_id
        assert context.user_id not in manager.connections_by_user
        assert "A001" not in manager.connections_by_account
        assert "B001" not in manager.connections_by_account

    asyncio.run(scenario())
