import logging
import os
import signal
import socket
from dataclasses import dataclass, replace
from threading import Event, Lock

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.logging_config import setup_logging
from app.core.redis_client import redis_client
from app.infrastructure.market_tick_stream_consumer import (
    MarketStreamMessage,
    MarketTickStreamConsumer,
)
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.infrastructure.redis_keys import pnl_event_failure_key
from app.services.active_position_cache import ActivePositionCache
from app.services.realtime_pnl_service import (
    PnlEventValidationError,
    RealtimePnlService,
)


logger = logging.getLogger(__name__)


def generate_consumer_name() -> str:
    return f"pnl-consumer-{socket.gethostname()}-{os.getpid()}"


@dataclass(frozen=True)
class PnlWorkerStats:
    """PnL Worker累计运行统计。"""

    ticks_processed: int = 0
    positions_calculated: int = 0
    redis_snapshots_written: int = 0
    dirty_positions: int = 0
    postgres_positions_persisted: int = 0
    postgres_accounts_persisted: int = 0
    skipped_ticks: int = 0
    failed_ticks: int = 0


class RealtimePnlWorker:
    """独立消费完整实时行情并计算Redis盈亏快照的单实例Worker。"""

    def __init__(
        self,
        *,
        stream_consumer: MarketTickStreamConsumer,
        service: RealtimePnlService,
        batch_size: int,
        block_ms: int,
        pending_idle_ms: int,
        max_retries: int,
        retry_interval_seconds: float,
    ):
        self.stream_consumer = stream_consumer
        self.service = service
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.pending_idle_ms = pending_idle_ms
        self.max_retries = max_retries
        self.retry_interval_seconds = retry_interval_seconds
        self.stop_event = Event()
        self._stats = PnlWorkerStats()
        self._stats_lock = Lock()

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def stats_snapshot(self) -> PnlWorkerStats:
        with self._stats_lock:
            return self._stats

    def _update_stats(self, **changes: int) -> None:
        with self._stats_lock:
            values = {
                name: getattr(self._stats, name) + value
                for name, value in changes.items()
            }
            self._stats = replace(self._stats, **values)

    def _dead_letter(
        self,
        message_id: str,
        fields: dict[str, str],
        error: str,
    ) -> None:
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
        fields: dict[str, str] | None,
    ) -> str:
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
            changes = {
                "ticks_processed": 1,
                "positions_calculated": result.positions_calculated,
                "redis_snapshots_written": (
                    result.redis_snapshots_written
                ),
                "dirty_positions": result.dirty_positions,
            }
            if result.action in {"SKIPPED", "NO_POSITION"}:
                changes["skipped_ticks"] = 1
            self._update_stats(**changes)
            return "acknowledged"
        except PnlEventValidationError as exc:
            try:
                self._dead_letter(message_id, fields, str(exc))
                self._update_stats(failed_ticks=1)
                return "dead_lettered"
            except Exception:
                logger.exception("PnL非法行情写入死信失败 id=%s", message_id)
                self._update_stats(failed_ticks=1)
                return "retry"
        except Exception as exc:
            self._update_stats(failed_ticks=1)
            try:
                failures = self.stream_consumer.increment_failure(
                    message_id
                )
                if failures >= self.max_retries:
                    self._dead_letter(
                        message_id,
                        fields,
                        f"{type(exc).__name__}: {exc}",
                    )
                    logger.error(
                        "PnL行情超过重试上限并进入死信 id=%s",
                        message_id,
                    )
                    return "dead_lettered"
            except Exception:
                logger.exception("PnL行情失败状态记录异常 id=%s", message_id)
                return "retry"
            logger.warning(
                "PnL行情处理失败，保留Pending id=%s retry_count=%s",
                message_id,
                failures,
            )
            return "retry"

    def _process(self, messages: list[MarketStreamMessage]) -> None:
        for message_id, fields in messages:
            self.handle_message(message_id, fields)

    def run_once(self) -> None:
        self._process(
            self.stream_consumer.claim_stale_messages(
                pending_idle_ms=self.pending_idle_ms,
                batch_size=self.batch_size,
            )
        )
        self._process(
            self.stream_consumer.read_new_messages(
                batch_size=self.batch_size,
                block_ms=self.block_ms,
            )
        )

    def run_forever(self) -> None:
        group_ready = False
        while not self.stop_event.is_set():
            try:
                if not group_ready:
                    self.stream_consumer.ensure_group()
                    group_ready = True
                    logger.info(
                        "PnL Consumer Group已就绪 stream=%s group=%s "
                        "consumer=%s",
                        self.stream_consumer.stream_name,
                        self.stream_consumer.group_name,
                        self.stream_consumer.consumer_name,
                    )
                self.run_once()
            except Exception:
                logger.exception("PnL行情消费循环异常")
                self.stop_event.wait(self.retry_interval_seconds)


def build_worker() -> RealtimePnlWorker:
    pnl_store = RealtimePnlStore(redis_client)
    consumer = MarketTickStreamConsumer(
        redis_client,
        stream_name=settings.market_tick_stream_name,
        group_name=settings.pnl_consumer_group,
        consumer_name=(
            settings.pnl_consumer_name or generate_consumer_name()
        ),
        dead_letter_stream=settings.pnl_dead_letter_stream,
        failure_ttl_seconds=settings.pnl_failure_ttl_seconds,
        failure_key_factory=pnl_event_failure_key,
    )
    cache = ActivePositionCache(
        session_factory=SessionLocal,
        refresh_ms=settings.active_position_cache_refresh_ms,
        version_loader=pnl_store.get_position_cache_version,
    )
    service = RealtimePnlService(
        active_position_cache=cache,
        pnl_store=pnl_store,
    )
    return RealtimePnlWorker(
        stream_consumer=consumer,
        service=service,
        batch_size=settings.pnl_consumer_batch_size,
        block_ms=settings.pnl_consumer_block_ms,
        pending_idle_ms=settings.pnl_pending_idle_ms,
        max_retries=settings.pnl_event_max_retries,
        retry_interval_seconds=(
            settings.pnl_consumer_retry_interval_seconds
        ),
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
