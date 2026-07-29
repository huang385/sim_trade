import json
import logging
import os
import signal
import socket
from dataclasses import dataclass
from threading import Event
from typing import Callable, Mapping

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.logging_config import setup_logging
from app.core.redis_client import redis_client
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.order_stream_consumer import (
    OrderStreamConsumer,
    StreamMessage,
)
from app.matching.registry import create_matching_engine
from app.repositories.order_repository import OrderRepository
from app.services.accepted_order_event_service import (
    AcceptedOrderEventService,
    UnsupportedOrderEventError,
)
from app.services.live_market_snapshot_service import (
    LiveMarketSnapshotService,
)
from app.services.market_tick_matching_service import (
    MarketTickMatchingService,
)
from app.services.order_arrival_matching_service import (
    OrderArrivalMatchingService,
)
from app.services.trade_settlement_service import TradeSettlementService


logger = logging.getLogger(__name__)


def generate_consumer_name() -> str:
    """根据主机名和进程号生成多进程环境下唯一的 Consumer 名称。"""

    return f"order-consumer-{socket.gethostname()}-{os.getpid()}"


@dataclass(frozen=True)
class ConsumerRunResult:
    """单轮消费结果，便于日志、测试和运行监控。"""

    received: int = 0
    acknowledged: int = 0
    retried: int = 0
    dead_lettered: int = 0


class OrderEventConsumerWorker:
    """
    消费订单 Stream，并按数据库最新状态维护 Redis 活动订单。

    Worker 只协调 Session、ACK、重试、Pending 恢复和退出信号；业务判断
    由 AcceptedOrderEventService 完成，Redis Group 操作由基础设施层完成。
    ORDER_ACCEPTED 和部分成交会新增或更新索引，FILLED 等终态会删除索引；
    同一 Stream 中的 TRADE_CREATED 是已知透传事件，不会误入死信。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        stream_consumer: OrderStreamConsumer,
        event_service: AcceptedOrderEventService,
        arrival_matching_service: OrderArrivalMatchingService | None = None,
        batch_size: int,
        block_ms: int,
        pending_idle_ms: int,
        max_retries: int,
        retry_interval_seconds: float,
    ):
        self.session_factory = session_factory
        self.stream_consumer = stream_consumer
        self.event_service = event_service
        self.arrival_matching_service = arrival_matching_service
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.pending_idle_ms = pending_idle_ms
        self.max_retries = max_retries
        self.retry_interval_seconds = retry_interval_seconds
        self.stop_event = Event()

    @property
    def consumer_name(self) -> str:
        return self.stream_consumer.consumer_name

    def request_stop(self, *_args) -> None:
        """请求 Worker 在当前 Redis 阻塞读取结束后平滑退出。"""

        self.stop_event.set()

    @staticmethod
    def _message_context(
        fields: Mapping[str, str] | None,
    ) -> tuple[str, str]:
        """尽力提取日志需要的 event_id 和 order_id，坏消息也不会再次报错。"""

        if fields is None:
            return "", ""
        event_id = fields.get("event_id", "")
        order_id = ""
        try:
            payload = json.loads(fields.get("payload", ""))
            if isinstance(payload, dict):
                order_id = str(payload.get("order_id", ""))
        except (TypeError, json.JSONDecodeError):
            pass
        return event_id, order_id

    def _dead_letter_and_ack(
        self,
        message_id: str,
        fields: Mapping[str, str],
        error: str,
    ) -> None:
        """先可靠写入死信 Stream，再确认原消息。"""

        self.stream_consumer.publish_dead_letter(
            source_message_id=message_id,
            fields=fields,
            error=error,
        )
        self.stream_consumer.acknowledge(message_id)
        self.stream_consumer.clear_failure(message_id)

    def handle_message(
        self,
        message_id: str,
        fields: Mapping[str, str] | None,
    ) -> str:
        """
        处理一条消息，返回 acknowledged、retry 或 dead_lettered。

        所有异常都在单条消息范围内处理，不允许使整个 Worker 永久退出。
        """

        # Redis 5 对正文已删除但仍停留在PEL中的消息返回 None。此时已经没有
        # 可供业务处理或写入死信的内容，确认墓碑消息即可解除永久Pending。
        if fields is None:
            try:
                self.stream_consumer.acknowledge(message_id)
                self.stream_consumer.clear_failure(message_id)
                logger.warning(
                    "已清理正文不存在的Pending消息 stream_message_id=%s "
                    "consumer_name=%s",
                    message_id,
                    self.consumer_name,
                )
                return "acknowledged"
            except Exception:
                logger.exception(
                    "清理Pending墓碑消息失败 stream_message_id=%s "
                    "consumer_name=%s",
                    message_id,
                    self.consumer_name,
                )
                return "retry"

        event_id, order_id = self._message_context(fields)
        try:
            with self.session_factory() as db:
                try:
                    result = self.event_service.process(db, fields)
                except Exception:
                    db.rollback()
                    raise

            arrival_result = None
            if (
                self.arrival_matching_service is not None
                and result.event_type == "ORDER_ACCEPTED"
                and result.action in {"REGISTERED", "DUPLICATE"}
            ):
                # 活动索引已经完成原子写入后，才允许用当前代WebSocket盘口
                # 触发一次撮合。无可用盘口是正常等待，数据库或Redis异常则
                # 保留订单事件Pending，重试仍由成交幂等约束保护。
                arrival_result = (
                    self.arrival_matching_service.match_if_ready(
                        order_id=result.order_id,
                        exchange_id=result.exchange_id,
                        symbol=result.symbol,
                        order_snapshot=getattr(
                            result,
                            "order_snapshot",
                            None,
                        ),
                    )
                )

            self.stream_consumer.acknowledge(message_id)
            self.stream_consumer.clear_failure(message_id)
            logger.info(
                "订单事件处理成功 stream_message_id=%s event_id=%s "
                "order_id=%s consumer_name=%s action=%s arrival_match=%s",
                message_id,
                result.event_id,
                result.order_id,
                self.consumer_name,
                result.action,
                (
                    arrival_result.action
                    if arrival_result is not None
                    else "NOT_APPLICABLE"
                ),
            )
            return "acknowledged"

        except UnsupportedOrderEventError as exc:
            # 未知事件类型即使重试也不会变为可处理，直接进入死信。
            try:
                self._dead_letter_and_ack(message_id, fields, str(exc))
            except Exception:
                logger.exception(
                    "订单事件写入死信失败 stream_message_id=%s event_id=%s "
                    "order_id=%s consumer_name=%s",
                    message_id,
                    event_id,
                    order_id,
                    self.consumer_name,
                )
                return "retry"
            return "dead_lettered"

        except Exception as exc:
            try:
                failure_count = self.stream_consumer.increment_failure(
                    message_id
                )
                if failure_count >= self.max_retries:
                    self._dead_letter_and_ack(
                        message_id,
                        fields,
                        f"{type(exc).__name__}: {exc}",
                    )
                    logger.error(
                        "订单事件超过重试上限并进入死信 "
                        "stream_message_id=%s event_id=%s order_id=%s "
                        "consumer_name=%s retry_count=%s",
                        message_id,
                        event_id,
                        order_id,
                        self.consumer_name,
                        failure_count,
                    )
                    return "dead_lettered"
            except Exception:
                # Redis 不可用时无法计数或写死信，但必须保持原消息未 ACK。
                logger.exception(
                    "订单事件失败状态记录异常 stream_message_id=%s "
                    "event_id=%s order_id=%s consumer_name=%s",
                    message_id,
                    event_id,
                    order_id,
                    self.consumer_name,
                )
                return "retry"

            logger.warning(
                "订单事件处理失败，保留Pending等待重试 "
                "stream_message_id=%s event_id=%s order_id=%s "
                "consumer_name=%s retry_count=%s error=%s",
                message_id,
                event_id,
                order_id,
                self.consumer_name,
                failure_count,
                exc,
            )
            return "retry"

    def _process_messages(self, messages: list[StreamMessage]) -> ConsumerRunResult:
        """逐条处理一批消息，单条失败不影响其余消息。"""

        acknowledged = retried = dead_lettered = 0
        for message_id, fields in messages:
            result = self.handle_message(message_id, fields)
            acknowledged += result == "acknowledged"
            retried += result == "retry"
            dead_lettered += result == "dead_lettered"
        return ConsumerRunResult(
            received=len(messages),
            acknowledged=acknowledged,
            retried=retried,
            dead_lettered=dead_lettered,
        )

    def run_once(self) -> ConsumerRunResult:
        """先恢复超时 Pending，再阻塞读取并处理一批新消息。"""

        pending_messages = self.stream_consumer.claim_stale_messages(
            pending_idle_ms=self.pending_idle_ms,
            batch_size=self.batch_size,
        )
        pending_result = self._process_messages(pending_messages)

        new_messages = self.stream_consumer.read_new_messages(
            batch_size=self.batch_size,
            block_ms=self.block_ms,
        )
        new_result = self._process_messages(new_messages)
        return ConsumerRunResult(
            received=pending_result.received + new_result.received,
            acknowledged=(
                pending_result.acknowledged + new_result.acknowledged
            ),
            retried=pending_result.retried + new_result.retried,
            dead_lettered=(
                pending_result.dead_lettered + new_result.dead_lettered
            ),
        )

    def run_forever(self) -> None:
        """持续消费订单事件，Redis/PostgreSQL 临时故障时等待后重试。"""

        group_ready = False
        while not self.stop_event.is_set():
            try:
                if not group_ready:
                    self.stream_consumer.ensure_group()
                    group_ready = True
                    logger.info(
                        "订单Consumer Group已就绪 stream=%s group=%s "
                        "consumer_name=%s",
                        self.stream_consumer.stream_name,
                        self.stream_consumer.group_name,
                        self.consumer_name,
                    )
                self.run_once()
            except Exception:
                logger.exception(
                    "订单事件消费循环异常 consumer_name=%s",
                    self.consumer_name,
                )
                self.stop_event.wait(self.retry_interval_seconds)


def main() -> None:
    """命令行入口：python -m app.workers.order_event_consumer_worker。"""

    setup_logging()
    consumer_name = settings.order_consumer_name or generate_consumer_name()
    stream_consumer = OrderStreamConsumer(
        redis_client,
        stream_name=settings.order_stream_name,
        group_name=settings.order_consumer_group,
        consumer_name=consumer_name,
        dead_letter_stream=settings.order_dead_letter_stream,
        failure_ttl_seconds=settings.order_event_failure_ttl_seconds,
    )
    active_order_index = ActiveOrderIndex(redis_client)
    event_service = AcceptedOrderEventService(
        order_repository=OrderRepository(),
        active_order_index=active_order_index,
        processed_ttl_seconds=settings.order_event_processed_ttl_seconds,
    )
    matching_service = MarketTickMatchingService(
        session_factory=SessionLocal,
        active_order_index=active_order_index,
        order_repository=OrderRepository(),
        matching_engine=create_matching_engine(
            settings.matching_engine_name
        ),
        settlement_service=TradeSettlementService(),
    )
    arrival_matching_service = OrderArrivalMatchingService(
        live_market_snapshot_service=LiveMarketSnapshotService(
            redis_client
        ),
        matching_service=matching_service,
    )
    worker = OrderEventConsumerWorker(
        session_factory=SessionLocal,
        stream_consumer=stream_consumer,
        event_service=event_service,
        arrival_matching_service=arrival_matching_service,
        batch_size=settings.order_consumer_batch_size,
        block_ms=settings.order_consumer_block_ms,
        pending_idle_ms=settings.order_pending_idle_ms,
        max_retries=settings.order_event_max_retries,
        retry_interval_seconds=(
            settings.order_consumer_retry_interval_seconds
        ),
    )
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)
    try:
        worker.run_forever()
    finally:
        redis_client.close()


if __name__ == "__main__":
    main()
