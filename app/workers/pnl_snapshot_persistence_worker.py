import logging
import signal
from dataclasses import dataclass
from threading import Event

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.logging_config import setup_logging
from app.core.redis_client import redis_client
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.services.pnl_snapshot_persistence_service import (
    PnlSnapshotPersistenceService,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PnlPersistenceStats:
    """持久化Worker累计统计。"""

    postgres_positions_persisted: int = 0
    postgres_accounts_persisted: int = 0
    retained_dirty_positions: int = 0
    failed_batches: int = 0


class PnlSnapshotPersistenceWorker:
    """按可配置周期批量持久化Dirty持仓，Event.wait支持平滑停止。"""

    def __init__(
        self,
        *,
        service: PnlSnapshotPersistenceService,
        interval_ms: int,
        batch_size: int,
    ):
        self.service = service
        self.interval_seconds = max(interval_ms, 1) / 1000
        self.batch_size = batch_size
        self.stop_event = Event()
        self.stats = PnlPersistenceStats()

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def run_once(self):
        result = self.service.persist_batch(self.batch_size)
        self.stats = PnlPersistenceStats(
            postgres_positions_persisted=(
                self.stats.postgres_positions_persisted
                + result.positions_persisted
            ),
            postgres_accounts_persisted=(
                self.stats.postgres_accounts_persisted
                + result.accounts_persisted
            ),
            retained_dirty_positions=result.retained,
            failed_batches=self.stats.failed_batches,
        )
        if result.positions_persisted:
            logger.info(
                "PnL快照持久化完成 positions=%s accounts=%s retained=%s",
                result.positions_persisted,
                result.accounts_persisted,
                result.retained,
            )
        return result

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                self.stats = PnlPersistenceStats(
                    postgres_positions_persisted=(
                        self.stats.postgres_positions_persisted
                    ),
                    postgres_accounts_persisted=(
                        self.stats.postgres_accounts_persisted
                    ),
                    retained_dirty_positions=(
                        self.stats.retained_dirty_positions
                    ),
                    failed_batches=self.stats.failed_batches + 1,
                )
                logger.exception("PnL快照持久化批次异常")
            self.stop_event.wait(self.interval_seconds)


def build_worker() -> PnlSnapshotPersistenceWorker:
    pnl_store = RealtimePnlStore(redis_client)
    service = PnlSnapshotPersistenceService(
        session_factory=SessionLocal,
        pnl_store=pnl_store,
        market_tick_store=MarketTickStore(
            redis_client,
            stream_name=settings.market_tick_stream_name,
        ),
    )
    return PnlSnapshotPersistenceWorker(
        service=service,
        interval_ms=settings.pnl_persist_interval_ms,
        batch_size=settings.pnl_persist_batch_size,
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
