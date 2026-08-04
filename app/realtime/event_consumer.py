import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.core.config import settings
from app.infrastructure.order_stream_consumer import OrderStreamConsumer
from app.realtime.event_router import RealtimeEventRouter
from app.realtime.gateway_lease import GatewayLease


logger = logging.getLogger(__name__)


class RealtimeEventConsumer:
    """Gateway内唯一实时事件Consumer；阻塞Redis读取移到工作线程。"""

    def __init__(
        self,
        *,
        consumer: OrderStreamConsumer,
        router: RealtimeEventRouter,
        lease: GatewayLease,
        owner_id: str,
        on_lease_lost: Callable[[], Awaitable[None]],
    ):
        self.consumer = consumer
        self.router = router
        self.lease = lease
        self.owner_id = owner_id
        self.on_lease_lost = on_lease_lost
        self.running = False

    async def _ensure_owner(self) -> bool:
        """消费和路由前确认租约；失败时立即关闭旧Gateway连接。"""

        owned = await asyncio.to_thread(
            self.lease.is_owner,
            self.owner_id,
        )
        if owned:
            return True
        self.running = False
        await self.on_lease_lost()
        return False

    async def _acknowledge(self, message_id: str) -> bool:
        """ACK与owner校验位于同一Lua原子操作中。"""

        owned, _ = await asyncio.to_thread(
            self.lease.acknowledge_if_owned,
            owner_id=self.owner_id,
            stream_name=self.consumer.stream_name,
            group_name=self.consumer.group_name,
            message_ids=[message_id],
        )
        if owned:
            return True
        self.running = False
        await self.on_lease_lost()
        return False

    async def _handle(
        self,
        message_id: str,
        fields: dict[str, str] | None,
    ) -> None:
        if not await self._ensure_owner():
            return
        if fields is None:
            await self._acknowledge(message_id)
            return
        try:
            if not await self._ensure_owner():
                return
            await self.router.route(message_id, fields)
            await asyncio.to_thread(self.consumer.clear_failure, message_id)
            await self._acknowledge(message_id)
        except Exception as exc:
            if not await self._ensure_owner():
                return
            failures = await asyncio.to_thread(
                self.consumer.increment_failure,
                message_id,
            )
            if failures < 10:
                logger.warning(
                    "Gateway实时事件路由失败 message_id=%s retry=%s",
                    message_id,
                    failures,
                )
                return
            await asyncio.to_thread(
                self.consumer.publish_dead_letter,
                source_message_id=message_id,
                fields=fields,
                error=f"{type(exc).__name__}: {exc}",
            )
            await asyncio.to_thread(self.consumer.clear_failure, message_id)
            if not await self._acknowledge(message_id):
                return
            logger.error("Gateway实时事件进入死信 message_id=%s", message_id)

    async def run(self) -> None:
        await asyncio.to_thread(self.consumer.ensure_group)
        self.running = True
        while self.running:
            try:
                if not await self._ensure_owner():
                    return
                pending = await asyncio.to_thread(
                    self.consumer.claim_stale_messages,
                    pending_idle_ms=settings.ws_gateway_pending_idle_ms,
                    batch_size=settings.ws_gateway_batch_size,
                )
                messages = pending or await asyncio.to_thread(
                    self.consumer.read_new_messages,
                    batch_size=settings.ws_gateway_batch_size,
                    block_ms=settings.ws_gateway_block_ms,
                )
                for message_id, fields in messages:
                    if not self.running:
                        break
                    await self._handle(message_id, fields)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Gateway实时事件消费循环异常")
                await asyncio.sleep(1)

    def stop(self) -> None:
        self.running = False
