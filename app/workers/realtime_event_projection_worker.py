import logging
import signal
from threading import Event

from app.core.config import settings
from app.core.redis_client import redis_client
from app.infrastructure.order_stream_consumer import OrderStreamConsumer
from app.infrastructure.redis_keys import (
    ORDER_EVENT_STREAM,
    REALTIME_PROJECTION_CONSUMER_GROUP,
    REALTIME_PROJECTION_DEAD_LETTER_STREAM,
    realtime_projection_failure_key,
)
from app.modules.realtime import (
    RealtimeEventProjectionService,
)
from app.modules.realtime import RealtimeEventStore


logger = logging.getLogger(__name__)


class RealtimeEventProjectionWorker:
    """把已提交订单Outbox事件转换为统一WebSocket事件流。"""

    def __init__(
        self,
        *,
        consumer: OrderStreamConsumer,
        event_store: RealtimeEventStore,
        batch_size: int | None = None,
        block_ms: int | None = None,
        pending_idle_ms: int | None = None,
        max_retries: int | None = None,
    ):
        self.consumer = consumer
        self.event_store = event_store
        self.batch_size = batch_size or settings.realtime_projection_batch_size
        self.block_ms = block_ms or settings.realtime_projection_block_ms
        self.pending_idle_ms = (
            pending_idle_ms or settings.realtime_projection_pending_idle_ms
        )
        self.max_retries = max_retries or settings.realtime_projection_max_retries
        self.stop_event = Event()

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def _handle(self, message_id: str, fields: dict[str, str] | None) -> None:
        if fields is None:
            # Redis 5已删除正文的PEL墓碑只能ACK清理，不能解引用fields。
            self.consumer.acknowledge(message_id)
            return
        try:
            envelope = RealtimeEventProjectionService.project(
                source_message_id=message_id,
                fields=fields,
            )
            self.event_store.publish_projected_once(envelope)
            self.consumer.clear_failure(message_id)
            self.consumer.acknowledge(message_id)
        except Exception as exc:
            failures = self.consumer.increment_failure(message_id)
            if failures < self.max_retries:
                logger.warning(
                    "实时事件投影失败 message_id=%s retry=%s",
                    message_id,
                    failures,
                )
                return
            self.consumer.publish_dead_letter(
                source_message_id=message_id,
                fields=fields,
                error=f"{type(exc).__name__}: {exc}",
            )
            self.consumer.clear_failure(message_id)
            self.consumer.acknowledge(message_id)
            logger.error("实时事件投影进入死信 message_id=%s", message_id)

    def run_once(self) -> int:
        pending = self.consumer.claim_stale_messages(
            pending_idle_ms=self.pending_idle_ms,
            batch_size=self.batch_size,
        )
        messages = pending or self.consumer.read_new_messages(
            batch_size=self.batch_size,
            block_ms=self.block_ms,
        )
        for message_id, fields in messages:
            self._handle(message_id, fields)
        return len(messages)

    def run_forever(self) -> None:
        self.consumer.ensure_group()
        logger.info(
            "实时事件投影Worker已启动 stream=%s group=%s",
            self.consumer.stream_name,
            self.consumer.group_name,
        )
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("实时事件投影消费循环异常")
                self.stop_event.wait(1.0)


def build_worker() -> RealtimeEventProjectionWorker:
    import os
    import socket

    consumer_name = settings.realtime_projection_consumer_name or (
        f"realtime-projection-{socket.gethostname()}-{os.getpid()}"
    )
    consumer = OrderStreamConsumer(
        redis_client,
        stream_name=ORDER_EVENT_STREAM,
        group_name=REALTIME_PROJECTION_CONSUMER_GROUP,
        consumer_name=consumer_name,
        dead_letter_stream=REALTIME_PROJECTION_DEAD_LETTER_STREAM,
        failure_ttl_seconds=(
            settings.realtime_projection_failure_ttl_seconds
        ),
        failure_key_factory=realtime_projection_failure_key,
        # 首次上线由完整快照覆盖历史事实，只投影建组后的新Outbox事件，
        # 避免历史接单消息在新快照之后以新Stream编号覆盖终态订单。
        group_start_id="$",
    )
    return RealtimeEventProjectionWorker(
        consumer=consumer,
        event_store=RealtimeEventStore(redis_client),
    )


def main() -> None:
    worker = build_worker()
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)
    try:
        worker.run_forever()
    finally:
        redis_client.close()


if __name__ == "__main__":
    main()
