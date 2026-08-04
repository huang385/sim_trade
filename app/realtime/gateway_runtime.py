import asyncio
import os
import socket
from uuid import uuid4

from app.core.config import settings
from app.core.redis_client import redis_client
from app.infrastructure.order_stream_consumer import OrderStreamConsumer
from app.infrastructure.redis_keys import (
    REALTIME_EVENT_STREAM,
    WS_GATEWAY_CONSUMER_GROUP,
    WS_GATEWAY_DEAD_LETTER_STREAM,
    websocket_delivery_failure_key,
)
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.realtime.connection_manager import ConnectionManager
from app.realtime.event_consumer import RealtimeEventConsumer
from app.realtime.event_router import RealtimeEventRouter
from app.realtime.event_store import RealtimeEventStore
from app.realtime.gateway_lease import GatewayLease
from app.realtime.metrics import realtime_metrics
from app.realtime.snapshot_service import SnapshotService
from app.realtime.subscription_service import SubscriptionService
from app.realtime.websocket_auth_service import WebSocketAuthService
from app.realtime.websocket_ticket_service import WebSocketTicketService
from app.services.account_authorization_service import (
    AccountAuthorizationService,
)


class GatewayRuntime:
    """组合Gateway依赖、单实例租约、事件消费和进程级生命周期。"""

    def __init__(self):
        self.owner_id = (
            f"ws-gateway-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        )
        self.manager = ConnectionManager()
        self.ticket_service = WebSocketTicketService(redis_client)
        self.auth_service = WebSocketAuthService()
        self.authorization_service = AccountAuthorizationService()
        self.subscription_service = SubscriptionService()
        self.event_store = RealtimeEventStore(redis_client)
        self.snapshot_service = SnapshotService(
            RealtimePnlStore(redis_client)
        )
        self.lease = GatewayLease(redis_client)
        consumer_name = settings.ws_gateway_consumer_name or self.owner_id
        stream_consumer = OrderStreamConsumer(
            redis_client,
            stream_name=REALTIME_EVENT_STREAM,
            group_name=WS_GATEWAY_CONSUMER_GROUP,
            consumer_name=consumer_name,
            dead_letter_stream=WS_GATEWAY_DEAD_LETTER_STREAM,
            failure_ttl_seconds=settings.order_event_failure_ttl_seconds,
            failure_key_factory=websocket_delivery_failure_key,
            # 首次连接由完整快照建立当前状态，不重放最多百万条历史增量。
            # 已存在Group时ensure_group的BUSYGROUP分支不会重置原游标。
            group_start_id="$",
        )
        self.consumer = RealtimeEventConsumer(
            consumer=stream_consumer,
            router=RealtimeEventRouter(self.manager),
            lease=self.lease,
            owner_id=self.owner_id,
            on_lease_lost=self._handle_lease_lost,
        )
        self.active = False
        self.consumer_task: asyncio.Task | None = None
        self.lease_task: asyncio.Task | None = None

    async def start(self) -> None:
        if (
            settings.ws_send_queue_size <= 0
            or settings.ws_snapshot_buffer_size <= 0
            or settings.ws_max_connections_per_user <= 0
            or settings.ws_max_subscriptions_per_connection <= 0
            or settings.ws_heartbeat_interval_seconds <= 0
            or settings.ws_heartbeat_timeout_seconds
            <= settings.ws_heartbeat_interval_seconds
            or settings.ws_gateway_lease_renew_seconds <= 0
            or settings.ws_gateway_lease_renew_seconds
            >= settings.ws_gateway_lease_ttl_seconds
        ):
            raise RuntimeError("WebSocket Gateway运行参数不合法")
        acquired = await asyncio.to_thread(self.lease.acquire, self.owner_id)
        if not acquired:
            raise RuntimeError("已有活动WebSocket Gateway持有单实例租约")
        try:
            # 启动成功前先确认Consumer Group可用，不能带着已经退出的
            # 消费任务继续接受连接并伪装实时服务正常。
            await asyncio.to_thread(self.consumer.consumer.ensure_group)
        except Exception:
            await asyncio.to_thread(self.lease.release, self.owner_id)
            raise
        self.active = True
        realtime_metrics.set("ws_gateway_lease_status", 1)
        self.consumer_task = asyncio.create_task(
            self.consumer.run(),
            name="ws-realtime-event-consumer",
        )
        self.lease_task = asyncio.create_task(
            self._renew_lease(),
            name="ws-gateway-lease-renew",
        )

    async def _renew_lease(self) -> None:
        while self.active:
            await asyncio.sleep(settings.ws_gateway_lease_renew_seconds)
            try:
                renewed = await asyncio.to_thread(
                    self.lease.renew,
                    self.owner_id,
                )
            except Exception:
                renewed = False
            if renewed:
                continue
            await self._handle_lease_lost()
            return

    async def _handle_lease_lost(self) -> None:
        """幂等停止失去租约的实例；绝不释放其他owner的新租约。"""

        self.active = False
        realtime_metrics.set("ws_gateway_lease_status", 0)
        self.consumer.stop()
        await self.manager.shutdown()

    async def stop(self) -> None:
        self.active = False
        self.consumer.stop()
        if self.consumer_task:
            self.consumer_task.cancel()
        if self.lease_task:
            self.lease_task.cancel()
        tasks = [
            task
            for task in (self.consumer_task, self.lease_task)
            if task is not None and task is not asyncio.current_task()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.manager.shutdown()
        await asyncio.to_thread(self.lease.release, self.owner_id)
        realtime_metrics.set("ws_gateway_lease_status", 0)
