import asyncio
import logging
from collections import defaultdict

from app.core.config import settings
from app.realtime.connection_context import ConnectionContext
from app.realtime.event_enums import WebSocketCloseCode
from app.realtime.event_schema import RealtimeEventEnvelope
from app.realtime.metrics import realtime_metrics


logger = logging.getLogger(__name__)


def _stream_version(value: str) -> tuple[int, int]:
    """把Redis Stream编号转换为可比较元组，非法值按最旧版本处理。"""

    try:
        milliseconds, sequence = value.split("-", 1)
        return int(milliseconds), int(sequence)
    except (AttributeError, TypeError, ValueError):
        return 0, 0


class ConnectionManager:
    """维护进程内连接索引、有界队列、快照缓冲和安全清理。"""

    def __init__(
        self,
        *,
        max_connections_per_user: int | None = None,
        snapshot_buffer_size: int | None = None,
    ):
        self.max_connections_per_user = (
            max_connections_per_user
            if max_connections_per_user is not None
            else settings.ws_max_connections_per_user
        )
        self.snapshot_buffer_size = (
            snapshot_buffer_size
            if snapshot_buffer_size is not None
            else settings.ws_snapshot_buffer_size
        )
        self.connections_by_id: dict[str, ConnectionContext] = {}
        self.connections_by_user: dict[str, set[str]] = defaultdict(set)
        self.connections_by_account: dict[str, set[str]] = defaultdict(set)

    @property
    def active_count(self) -> int:
        return len(self.connections_by_id)

    async def register(self, context: ConnectionContext) -> bool:
        """注册连接并启动唯一发送协程，超过用户上限时返回False。"""

        user_connections = self.connections_by_user[context.user_id]
        if len(user_connections) >= self.max_connections_per_user:
            return False
        self.connections_by_id[context.connection_id] = context
        user_connections.add(context.connection_id)
        context.sender_task = asyncio.create_task(
            self._sender_loop(context),
            name=f"ws-sender-{context.connection_id}",
        )
        realtime_metrics.increment("ws_connections_opened")
        realtime_metrics.set("ws_active_connections", self.active_count)
        return True

    async def _sender_loop(self, context: ConnectionContext) -> None:
        """每条连接只由本任务调用send_text，避免并发发送帧。"""

        try:
            while not context.closing:
                message = await context.send_queue.get()
                await context.websocket.send_text(message)
                realtime_metrics.increment("ws_events_sent")
                realtime_metrics.set(
                    "ws_send_queue_depth",
                    context.send_queue.qsize(),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "WebSocket发送任务结束 connection_id=%s user_id=%s",
                context.connection_id,
                context.user_id,
                exc_info=True,
            )

    def subscribe(
        self,
        context: ConnectionContext,
        account_ids: set[str],
        *,
        snapshot_loading: bool,
    ) -> None:
        """先建立账户路由索引，再加载快照，消除快照期间的丢失窗口。"""

        for account_id in account_ids:
            context.subscribed_account_ids.add(account_id)
            self.connections_by_account[account_id].add(
                context.connection_id
            )
            if snapshot_loading:
                context.snapshot_loading_accounts.add(account_id)
                context.snapshot_buffers.setdefault(account_id, [])

    def unsubscribe(
        self,
        context: ConnectionContext,
        account_ids: set[str],
    ) -> None:
        for account_id in account_ids:
            context.subscribed_account_ids.discard(account_id)
            context.snapshot_loading_accounts.discard(account_id)
            context.snapshot_buffers.pop(account_id, None)
            context.last_versions.pop(account_id, None)
            connection_ids = self.connections_by_account.get(account_id)
            if connection_ids is not None:
                connection_ids.discard(context.connection_id)
                if not connection_ids:
                    self.connections_by_account.pop(account_id, None)

    async def enqueue(
        self,
        context: ConnectionContext,
        serialized: str,
    ) -> bool:
        """非阻塞加入有界队列；满队列会触发重同步并关闭慢连接。"""

        if context.closing:
            return False
        try:
            context.send_queue.put_nowait(serialized)
            realtime_metrics.set(
                "ws_send_queue_depth", context.send_queue.qsize()
            )
            return True
        except asyncio.QueueFull:
            realtime_metrics.increment("ws_resync_required")
            realtime_metrics.increment("ws_slow_connections_closed")
            await self.close(
                context,
                code=WebSocketCloseCode.SLOW_CONNECTION,
                reason="客户端消费过慢，需要重新同步",
            )
            return False

    async def broadcast_to_account(
        self,
        envelope: RealtimeEventEnvelope,
        serialized: str,
    ) -> int:
        """复用同一序列化结果路由到目标账户的全部连接。"""

        account_id = envelope.account_id
        if not account_id:
            return 0
        sent = 0
        for connection_id in tuple(
            self.connections_by_account.get(account_id, ())
        ):
            context = self.connections_by_id.get(connection_id)
            if context is None or account_id not in context.subscribed_account_ids:
                continue
            if account_id in context.snapshot_loading_accounts:
                buffer = context.snapshot_buffers.setdefault(account_id, [])
                if len(buffer) >= self.snapshot_buffer_size:
                    await self.close(
                        context,
                        code=WebSocketCloseCode.RESYNC_REQUIRED,
                        reason="快照期间事件积压，需要重新同步",
                    )
                    continue
                buffer.append((envelope.version, serialized))
                continue
            current = context.last_versions.get(account_id, "0-0")
            if _stream_version(envelope.version) <= _stream_version(current):
                realtime_metrics.increment("ws_duplicate_events")
                continue
            if await self.enqueue(context, serialized):
                context.last_versions[account_id] = envelope.version
                sent += 1
        return sent

    async def finish_snapshot(
        self,
        context: ConnectionContext,
        *,
        account_ids: set[str],
        cursor: str,
        snapshot_serialized: str,
    ) -> None:
        """先发完整快照，再按Stream版本发送快照读取期间的较新事件。"""

        if not await self.enqueue(context, snapshot_serialized):
            return
        for account_id in account_ids:
            context.last_versions[account_id] = cursor
            buffered = context.snapshot_buffers.pop(account_id, [])
            context.snapshot_loading_accounts.discard(account_id)
            for version, serialized in sorted(
                buffered,
                key=lambda item: _stream_version(item[0]),
            ):
                if _stream_version(version) <= _stream_version(cursor):
                    continue
                if await self.enqueue(context, serialized):
                    context.last_versions[account_id] = version

    async def close(
        self,
        context: ConnectionContext,
        *,
        code: int,
        reason: str,
    ) -> None:
        """幂等关闭连接，并清理用户、账户、任务和临时缓冲索引。"""

        if context.closing:
            return
        context.closing = True
        self.connections_by_id.pop(context.connection_id, None)
        user_connections = self.connections_by_user.get(context.user_id)
        if user_connections is not None:
            user_connections.discard(context.connection_id)
            if not user_connections:
                self.connections_by_user.pop(context.user_id, None)
        self.unsubscribe(context, set(context.subscribed_account_ids))
        current_task = asyncio.current_task()
        if context.sender_task and context.sender_task is not current_task:
            context.sender_task.cancel()
        try:
            await context.websocket.close(code=int(code), reason=reason[:123])
        except Exception:
            pass
        realtime_metrics.increment("ws_connections_closed")
        realtime_metrics.set("ws_active_connections", self.active_count)

    async def shutdown(self) -> None:
        """Gateway退出或失去租约时关闭全部连接。"""

        await asyncio.gather(
            *(
                self.close(
                    context,
                    code=WebSocketCloseCode.GATEWAY_SHUTDOWN,
                    reason="WebSocket Gateway正在关闭",
                )
                for context in tuple(self.connections_by_id.values())
            ),
            return_exceptions=True,
        )
