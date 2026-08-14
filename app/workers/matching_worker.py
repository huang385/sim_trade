import logging
import os
import signal
import socket
from dataclasses import dataclass
from threading import Event

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.logging_config import setup_logging
from app.core.redis_client import redis_client
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.market_tick_stream_consumer import (
    MarketStreamMessage,
    MarketTickStreamConsumer,
)
from app.matching.registry import create_matching_engine
from app.infrastructure.database.repository_adapters import OrderRepository
from app.modules.orders import (
    MarketTickEventValidationError,
    MarketTickMatchingService,
    UnsupportedMarketTickEventError,
)
from app.modules.trades import TradeSettlementService


logger = logging.getLogger(__name__)


def generate_consumer_name() -> str:
    """生成主机和进程维度唯一的撮合 Consumer 名称。"""

    return f"matching-consumer-{socket.gethostname()}-{os.getpid()}"


@dataclass(frozen=True)
class MatchingRunResult:
    """单轮撮合消费统计，便于测试、日志和运行监控。"""

    # 本轮收到的Pending和新消息总数
    received: int = 0
    # 正常处理并ACK的消息数
    acknowledged: int = 0
    # 发生临时错误、仍保留Pending的消息数
    retried: int = 0
    # 已可靠写入死信并ACK原消息的数量
    dead_lettered: int = 0


class MatchingWorker:
    """
    消费实时Tick并触发撮合的常驻进程协调器。

    Worker负责进程生命周期、ACK、失败计数、死信和Pending恢复；行情解析、
    订单撮合、资金结算分别交给对应Service，避免在循环代码里堆积业务逻辑。
    """

    def __init__(
        self,
        *,
        stream_consumer: MarketTickStreamConsumer,
        matching_service: MarketTickMatchingService,
        batch_size: int,
        block_ms: int,
        pending_idle_ms: int,
        max_retries: int,
        retry_interval_seconds: float,
    ):
        # 基础设施和业务服务通过构造函数注入，单元测试无需连接真实Redis和
        # PostgreSQL即可验证ACK、重试、死信和退出行为。
        self.stream_consumer = stream_consumer
        self.matching_service = matching_service
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
        """SIGINT/SIGTERM 仅设置标记，当前阻塞读取返回后平滑退出。"""

        self.stop_event.set()

    def _dead_letter_and_ack(
        self, message_id: str, fields: dict[str, str], error: str
    ) -> None:
        """严格先发布死信，成功后再ACK并清除失败计数。"""

        self.stream_consumer.publish_dead_letter(
            source_message_id=message_id, fields=fields, error=error
        )
        self.stream_consumer.acknowledge(message_id)
        self.stream_consumer.clear_failure(message_id)

    def handle_message(
        self, message_id: str, fields: dict[str, str] | None
    ) -> str:
        """成功完整处理才 ACK；数据库异常及死信写入异常均不 ACK。"""

        if fields is None:
            # Redis 5中原消息正文可能已被XDEL，但PEL墓碑仍然存在。此时没有
            # 业务数据可恢复，只能ACK原ID清理Pending，避免永久重复领取。
            try:
                self.stream_consumer.acknowledge(message_id)
                self.stream_consumer.clear_failure(message_id)
                logger.warning("已清理正文不存在的行情Pending消息 id=%s", message_id)
                return "acknowledged"
            except Exception:
                logger.exception("清理行情Pending墓碑失败 id=%s", message_id)
                return "retry"
        try:
            # matching_service只有在全部候选订单取得确定结果后才正常返回。
            result = self.matching_service.process(
                stream_message_id=message_id, fields=fields
            )
            self.stream_consumer.acknowledge(message_id)
            self.stream_consumer.clear_failure(message_id)
            if result.settled_count > 0:
                # 只有本轮真正提交了新成交时才记录INFO，便于交易监控直接
                # 关注有效成交，不被高频但未触发成交的行情日志淹没。
                logger.info(
                    "行情撮合产生成交 id=%s candidates=%s matched=%s "
                    "settled=%s idempotent=%s",
                    message_id,
                    result.candidate_count,
                    result.matched_count,
                    result.settled_count,
                    result.idempotent_count,
                )
            else:
                # 未成交和幂等重放都属于正常处理结果，降为DEBUG。全局默认
                # INFO级别下不会输出，需要排查逐Tick行为时再临时打开DEBUG。
                logger.debug(
                    "行情撮合处理完成，未产生新成交 id=%s candidates=%s "
                    "matched=%s settled=%s idempotent=%s",
                    message_id,
                    result.candidate_count,
                    result.matched_count,
                    result.settled_count,
                    result.idempotent_count,
                )
            return "acknowledged"
        except (UnsupportedMarketTickEventError, MarketTickEventValidationError) as exc:
            # 消息结构或来源固定错误，重试没有意义，直接尝试写死信。
            try:
                self._dead_letter_and_ack(message_id, fields, str(exc))
                return "dead_lettered"
            except Exception:
                logger.exception("非法行情写入死信失败 id=%s", message_id)
                return "retry"
        except Exception as exc:
            # 数据库、Redis或其他临时异常统一增加失败次数，但当前消息不ACK。
            try:
                failure_count = self.stream_consumer.increment_failure(message_id)
                if failure_count >= self.max_retries:
                    # 达到上限后也必须先成功写死信；死信失败仍保持原Pending。
                    self._dead_letter_and_ack(
                        message_id, fields, f"{type(exc).__name__}: {exc}"
                    )
                    logger.error(
                        "行情撮合超过重试上限并进入死信 id=%s retry_count=%s",
                        message_id,
                        failure_count,
                    )
                    return "dead_lettered"
            except Exception:
                logger.exception("行情撮合失败状态记录异常 id=%s", message_id)
                return "retry"
            logger.warning(
                "行情撮合失败，保留Pending id=%s retry_count=%s error=%s",
                message_id,
                failure_count,
                exc,
            )
            return "retry"

    def _process_messages(
        self, messages: list[MarketStreamMessage]
    ) -> MatchingRunResult:
        """逐条隔离处理一批消息，一条失败不会使整个Worker退出。"""

        acknowledged = retried = dead_lettered = 0
        for message_id, fields in messages:
            action = self.handle_message(message_id, fields)
            acknowledged += action == "acknowledged"
            retried += action == "retry"
            dead_lettered += action == "dead_lettered"
        return MatchingRunResult(
            received=len(messages),
            acknowledged=acknowledged,
            retried=retried,
            dead_lettered=dead_lettered,
        )

    def run_once(self) -> MatchingRunResult:
        """
        执行一轮消费：先恢复超时Pending，再阻塞读取新行情。

        先处理Pending可以持续推进崩溃遗留消息；新消息仍通过BLOCK在到达时
        立即唤醒，不会因为固定sleep增加撮合延迟。
        """

        pending = self._process_messages(
            self.stream_consumer.claim_stale_messages(
                pending_idle_ms=self.pending_idle_ms,
                batch_size=self.batch_size,
            )
        )
        new = self._process_messages(
            self.stream_consumer.read_new_messages(
                batch_size=self.batch_size, block_ms=self.block_ms
            )
        )
        return MatchingRunResult(
            received=pending.received + new.received,
            acknowledged=pending.acknowledged + new.acknowledged,
            retried=pending.retried + new.retried,
            dead_lettered=pending.dead_lettered + new.dead_lettered,
        )

    def run_forever(self) -> None:
        """持续运行直到收到停止信号，临时Redis故障不会让进程永久退出。"""

        group_ready = False
        while not self.stop_event.is_set():
            try:
                if not group_ready:
                    self.stream_consumer.ensure_group()
                    group_ready = True
                    logger.info(
                        "行情撮合Consumer Group已就绪 stream=%s group=%s consumer=%s",
                        self.stream_consumer.stream_name,
                        self.stream_consumer.group_name,
                        self.consumer_name,
                    )
                self.run_once()
            except Exception:
                logger.exception("行情撮合消费循环异常 consumer=%s", self.consumer_name)
                self.stop_event.wait(self.retry_interval_seconds)


def build_matching_worker() -> MatchingWorker:
    """
    根据配置创建完整的撮合 Worker。

    撮合引擎通过 Registry 在进程启动阶段创建一次并注入编排服务，
    后续每条 Tick 都复用同一实例。未知引擎名称会在这里明确抛错，
    避免 Worker 带着错误配置进入消费循环。
    """

    consumer_name = settings.market_matching_consumer_name or generate_consumer_name()
    consumer = MarketTickStreamConsumer(
        redis_client,
        stream_name=settings.market_tick_stream_name,
        group_name=settings.market_matching_consumer_group,
        consumer_name=consumer_name,
        dead_letter_stream=settings.market_matching_dead_letter_stream,
        failure_ttl_seconds=settings.market_matching_failure_ttl_seconds,
    )
    matching_service = MarketTickMatchingService(
        session_factory=SessionLocal,
        active_order_index=ActiveOrderIndex(redis_client),
        order_repository=OrderRepository(),
        matching_engine=create_matching_engine(settings.matching_engine_name),
        settlement_service=TradeSettlementService(),
    )
    return MatchingWorker(
        stream_consumer=consumer,
        matching_service=matching_service,
        batch_size=settings.market_matching_batch_size,
        block_ms=settings.market_matching_block_ms,
        pending_idle_ms=settings.market_matching_pending_idle_ms,
        max_retries=settings.market_matching_max_retries,
        retry_interval_seconds=settings.market_matching_retry_interval_seconds,
    )


def main() -> None:
    """命令行入口：python -m app.workers.matching_worker。"""

    setup_logging()
    worker = build_matching_worker()
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)
    try:
        worker.run_forever()
    finally:
        redis_client.close()


if __name__ == "__main__":
    main()
