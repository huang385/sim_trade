import logging
import signal
from dataclasses import dataclass
from threading import Event
from typing import Callable

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.enums.order_enums import OutboxStatus
from app.infrastructure.order_event_publisher import OrderEventPublisher
from app.repositories.outbox_repository import OutboxRepository


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishRunResult:
    """单轮发布结果，便于运行日志和自动化测试检查。"""

    claimed: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0


class OutboxPublisherWorker:
    """
    从 PostgreSQL 领取 Outbox 事件并发布到 Redis Stream。

    领取事务会先把事件改为 PROCESSING 并提交，随后逐条发布。领取状态带
    有超时租约，因此进程意外退出后事件仍能被其他 Worker 补发。发布采用
    至少一次语义，下游消费者必须按 event_id 去重。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        publisher: OrderEventPublisher,
        outbox_repository: OutboxRepository | None = None,
        batch_size: int = 100,
        idle_seconds: float = 1.0,
    ):
        self.session_factory = session_factory
        self.publisher = publisher
        self.outbox_repository = outbox_repository or OutboxRepository()
        self.batch_size = batch_size
        self.idle_seconds = idle_seconds
        self.stop_event = Event()

    def request_stop(self, *_args) -> None:
        """请求在当前轮处理结束后平滑退出。"""

        self.stop_event.set()

    def _claim_event_ids(self) -> list[str]:
        """在短事务中领取事件并返回事件编号列表。"""

        with self.session_factory() as db:
            try:
                events = self.outbox_repository.claim_pending_events(
                    db,
                    batch_size=self.batch_size,
                )
                event_ids = [event.event_id for event in events]
                db.commit()
                return event_ids
            except Exception:
                db.rollback()
                raise

    def _publish_one(self, event_id: str) -> str:
        """发布一个已经领取的事件，并持久化成功或重试状态。"""

        with self.session_factory() as db:
            event = self.outbox_repository.get_by_event_id(db, event_id)
            if event is None or event.status != OutboxStatus.PROCESSING.value:
                return "skipped"

            try:
                message_id = self.publisher.publish(event)
            except Exception as exc:
                # Redis 错误只改变 Outbox 重试状态，不能反向影响已提交订单。
                self.outbox_repository.mark_retry(
                    event,
                    error=f"{type(exc).__name__}: {exc}",
                )
                result = (
                    "failed"
                    if event.status == OutboxStatus.FAILED.value
                    else "retried"
                )
                db.commit()
                logger.warning(
                    "Outbox事件发布失败 event_id=%s retry_count=%s status=%s",
                    event.event_id,
                    event.retry_count,
                    event.status,
                )
                return result

            self.outbox_repository.mark_sent(event)
            db.commit()
            logger.info(
                "Outbox事件发布成功 event_id=%s redis_message_id=%s",
                event.event_id,
                message_id,
            )
            return "sent"

    def run_once(self) -> PublishRunResult:
        """执行一轮领取与发布；没有事件时立即返回。"""

        event_ids = self._claim_event_ids()
        sent = retried = failed = 0
        for event_id in event_ids:
            result = self._publish_one(event_id)
            sent += result == "sent"
            retried += result == "retried"
            failed += result == "failed"
        return PublishRunResult(
            claimed=len(event_ids),
            sent=sent,
            retried=retried,
            failed=failed,
        )

    def run_forever(self) -> None:
        """持续轮询待发布事件，直到收到停止信号。"""

        logger.info(
            "Outbox发布Worker已启动 batch_size=%s idle_seconds=%s",
            self.batch_size,
            self.idle_seconds,
        )
        while not self.stop_event.is_set():
            try:
                result = self.run_once()
                if result.claimed == 0:
                    self.stop_event.wait(self.idle_seconds)
            except Exception:
                # 数据库临时异常也不能导致进程永久退出，记录后进入下一轮。
                logger.exception("Outbox发布轮询失败")
                self.stop_event.wait(self.idle_seconds)
        logger.info("Outbox发布Worker已停止")


def main() -> None:
    """命令行入口：python -m app.workers.outbox_publisher_worker。"""

    publisher = OrderEventPublisher(redis_client)
    worker = OutboxPublisherWorker(
        session_factory=SessionLocal,
        publisher=publisher,
    )
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)
    try:
        worker.run_forever()
    finally:
        # redis-py 的连接池在独立进程退出时显式释放。
        redis_client.close()


if __name__ == "__main__":
    main()
