import logging
import os
import signal
import socket
from threading import Event

from app.core.config import settings
from app.core.logging_config import setup_logging
from app.core.redis_client import redis_client
from app.infrastructure.order_stream_consumer import OrderStreamConsumer
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.infrastructure.redis_keys import pnl_trade_event_failure_key
from app.services.trade_created_pnl_service import (
    TradeCreatedPnlService,
    TradeCreatedPnlValidationError,
)


logger = logging.getLogger(__name__)


def generate_consumer_name() -> str:
    return f"pnl-trade-consumer-{socket.gethostname()}-{os.getpid()}"


class TradeEventPnlWorker:
    """独立消费TRADE_CREATED，在成交提交后刷新或清零Redis盈亏快照。"""

    def __init__(
        self,
        *,
        stream_consumer: OrderStreamConsumer,
        service: TradeCreatedPnlService,
    ):
        self.stream_consumer = stream_consumer
        self.service = service
        self.stop_event = Event()

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def handle_message(self, message_id: str, fields) -> str:
        if fields is None:
            self.stream_consumer.acknowledge(message_id)
            self.stream_consumer.clear_failure(message_id)
            return "acknowledged"
        try:
            result = self.service.process(
                stream_message_id=message_id,
                fields=fields,
            )
            self.stream_consumer.acknowledge(message_id)
            self.stream_consumer.clear_failure(message_id)
            if result.action == "DIRTY_MARKED":
                logger.info(
                    "成交后PnL合约已标记Dirty id=%s version=%s",
                    message_id,
                    result.dirty_version,
                )
            return "acknowledged"
        except TradeCreatedPnlValidationError as exc:
            self.stream_consumer.publish_dead_letter(
                source_message_id=message_id,
                fields=fields,
                error=str(exc),
            )
            self.stream_consumer.acknowledge(message_id)
            self.stream_consumer.clear_failure(message_id)
            return "dead_lettered"
        except Exception as exc:
            failures = self.stream_consumer.increment_failure(message_id)
            if failures >= settings.pnl_trade_event_max_retries:
                self.stream_consumer.publish_dead_letter(
                    source_message_id=message_id,
                    fields=fields,
                    error=f"{type(exc).__name__}: {exc}",
                )
                self.stream_consumer.acknowledge(message_id)
                self.stream_consumer.clear_failure(message_id)
                logger.error("成交后PnL刷新超过重试上限 id=%s", message_id)
                return "dead_lettered"
            logger.warning(
                "成交后PnL刷新失败，保留Pending id=%s retry_count=%s",
                message_id,
                failures,
            )
            return "retry"

    def run_once(self) -> None:
        messages = self.stream_consumer.claim_stale_messages(
            pending_idle_ms=settings.pnl_trade_pending_idle_ms,
            batch_size=settings.pnl_trade_consumer_batch_size,
        )
        messages += self.stream_consumer.read_new_messages(
            batch_size=settings.pnl_trade_consumer_batch_size,
            block_ms=settings.pnl_trade_consumer_block_ms,
        )
        for message_id, fields in messages:
            self.handle_message(message_id, fields)

    def run_forever(self) -> None:
        group_ready = False
        while not self.stop_event.is_set():
            try:
                if not group_ready:
                    self.stream_consumer.ensure_group()
                    group_ready = True
                    logger.info(
                        "成交PnL Consumer Group已就绪 stream=%s group=%s",
                        self.stream_consumer.stream_name,
                        self.stream_consumer.group_name,
                    )
                self.run_once()
            except Exception:
                logger.exception("成交后PnL事件消费循环异常")
                self.stop_event.wait(
                    settings.pnl_trade_retry_interval_seconds
                )


def build_worker() -> TradeEventPnlWorker:
    store = RealtimePnlStore(redis_client)
    service = TradeCreatedPnlService(
        pnl_store=store,
    )
    consumer = OrderStreamConsumer(
        redis_client,
        stream_name=settings.order_stream_name,
        group_name=settings.pnl_trade_consumer_group,
        consumer_name=(
            settings.pnl_trade_consumer_name
            or generate_consumer_name()
        ),
        dead_letter_stream=settings.pnl_trade_dead_letter_stream,
        failure_ttl_seconds=settings.pnl_trade_failure_ttl_seconds,
        failure_key_factory=pnl_trade_event_failure_key,
    )
    return TradeEventPnlWorker(
        stream_consumer=consumer,
        service=service,
    )


def main() -> None:
    setup_logging()
    worker = build_worker()
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)
    try:
        worker.run_forever()
    finally:
        redis_client.close()


if __name__ == "__main__":
    main()
