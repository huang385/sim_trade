import logging
import signal
import time
from dataclasses import dataclass
from threading import Event

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.logging_config import setup_logging
from app.core.redis_client import redis_client
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.infrastructure.risk_store import RiskStore
from app.modules.realtime import (
    PnlPersistenceResult,
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
        max_batches_per_cycle: int = 10,
        time_budget_ms: int = 800,
        monotonic=time.monotonic,
    ):
        self.service = service
        self.interval_seconds = max(interval_ms, 1) / 1000
        self.batch_size = batch_size
        self.max_batches_per_cycle = max(max_batches_per_cycle, 1)
        self.time_budget_seconds = max(time_budget_ms, 1) / 1000
        self.monotonic = monotonic
        self.stop_event = Event()
        self.stats = PnlPersistenceStats()

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def run_once(self):
        """
        同一轮连续领取多个批次。

        单批500只是数据库锁定规模上限；Dirty超过500时无需固定等待下一秒，
        直到集合暂空、达到批次数上限或耗尽本轮时间预算才退出。
        """

        started = self.monotonic()
        total_positions = 0
        total_accounts = 0
        total_requested = 0
        retained = 0
        result = None
        for _batch_number in range(self.max_batches_per_cycle):
            result = self.service.persist_batch(self.batch_size)
            total_requested += result.requested
            total_positions += result.positions_persisted
            total_accounts += result.accounts_persisted
            retained = result.retained
            if result.requested == 0:
                break
            if result.positions_persisted == 0:
                # 数据库不可用或当前批次均无法更新时，留到下一周期重试，
                # 避免同一轮无进展地连续冲击数据库。
                break
            if (
                self.monotonic() - started
                >= self.time_budget_seconds
            ):
                break
        if result is None:
            return None
        self.stats = PnlPersistenceStats(
            postgres_positions_persisted=(
                self.stats.postgres_positions_persisted
                + total_positions
            ),
            postgres_accounts_persisted=(
                self.stats.postgres_accounts_persisted
                + total_accounts
            ),
            retained_dirty_positions=retained,
            failed_batches=self.stats.failed_batches,
        )
        if total_positions:
            logger.info(
                "PnL快照持久化完成 positions=%s accounts=%s retained=%s",
                total_positions,
                total_accounts,
                retained,
            )
        return PnlPersistenceResult(
            requested=total_requested,
            positions_persisted=total_positions,
            accounts_persisted=total_accounts,
            retained=retained,
        )

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
        risk_store=RiskStore(redis_client),
    )
    return PnlSnapshotPersistenceWorker(
        service=service,
        interval_ms=settings.pnl_persist_interval_ms,
        batch_size=settings.pnl_persist_batch_size,
        max_batches_per_cycle=(
            settings.pnl_persist_max_batches_per_cycle
        ),
        time_budget_ms=settings.pnl_persist_time_budget_ms,
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
