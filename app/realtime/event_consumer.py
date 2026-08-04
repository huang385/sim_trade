import asyncio
import logging

from app.core.config import settings
from app.infrastructure.order_stream_consumer import OrderStreamConsumer
from app.realtime.event_router import RealtimeEventRouter


logger = logging.getLogger(__name__)


class RealtimeEventConsumer:
    """Gateway内唯一实时事件Consumer；阻塞Redis读取移到工作线程。"""

    def __init__(
        self,
        *,
        consumer: OrderStreamConsumer,
        router: RealtimeEventRouter,
    ):
        self.consumer = consumer
        self.router = router
        self.running = False

    async def _handle(
        self,
        message_id: str,
        fields: dict[str, str] | None,
    ) -> None:
        if fields is None:
            await asyncio.to_thread(self.consumer.acknowledge, message_id)
            return
        try:
            await self.router.route(message_id, fields)
            await asyncio.to_thread(self.consumer.clear_failure, message_id)
            await asyncio.to_thread(self.consumer.acknowledge, message_id)
        except Exception as exc:
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
            await asyncio.to_thread(self.consumer.acknowledge, message_id)
            logger.error("Gateway实时事件进入死信 message_id=%s", message_id)

    async def run(self) -> None:
        await asyncio.to_thread(self.consumer.ensure_group)
        self.running = True
        while self.running:
            try:
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
