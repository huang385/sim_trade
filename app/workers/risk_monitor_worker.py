import logging
import os
import signal
import socket
import time
from dataclasses import dataclass
from threading import Event

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.logging_config import setup_logging
from app.core.redis_client import redis_client
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.risk_store import RiskStore
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.infrastructure.database.repository_adapters import (
    AccountRepository,
    RiskRepository,
)
from app.modules.risk import LiquidationService, RiskMonitorService
from app.modules.orders import (
    get_order_cancellation_service,
    get_order_service,
)
from app.modules.realtime import (
    PnlSnapshotPersistenceService,
)


logger = logging.getLogger(__name__)


@dataclass
class RiskMonitorStats:
    accounts_checked: int = 0
    accounts_warning: int = 0
    accounts_margin_deficit: int = 0
    open_orders_cancelled: int = 0
    liquidation_tasks_created: int = 0
    liquidation_orders_created: int = 0
    liquidation_orders_filled: int = 0
    accounts_recovered: int = 0
    valuation_unavailable: int = 0
    dirty_retained: int = 0
    worker_failures: int = 0
    calculation_duration_ms: int = 0


class RiskMonitorWorker:
    """500ms合并账户Dirty并串行执行风险处置的单写者Worker。"""

    def __init__(
        self,
        *,
        risk_store: RiskStore,
        monitor_service: RiskMonitorService,
        liquidation_service: LiquidationService,
        session_factory=SessionLocal,
        interval_ms: int = 500,
        batch_size: int = 100,
        max_actions_per_cycle: int = 10,
        lease_ttl_seconds: int = 15,
        lease_renew_seconds: int = 5,
        full_reconciliation_seconds: int = 60,
    ):
        self.risk_store = risk_store
        self.monitor_service = monitor_service
        self.liquidation_service = liquidation_service
        self.session_factory = session_factory
        self.interval_seconds = max(interval_ms, 1) / 1000
        self.batch_size = max(batch_size, 1)
        self.max_actions_per_cycle = max(max_actions_per_cycle, 1)
        self.lease_ttl_ms = max(lease_ttl_seconds, 1) * 1000
        self.lease_renew_seconds = max(lease_renew_seconds, 1)
        self.full_reconciliation_seconds = max(full_reconciliation_seconds, 1)
        self.owner = f"risk-{socket.gethostname()}-{os.getpid()}"
        self.stop_event = Event()
        self.stats = RiskMonitorStats()
        self.last_reconciliation = 0.0
        self.last_lease_renewal = 0.0

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def _recover_all_accounts_if_due(self) -> None:
        now = time.monotonic()
        if now - self.last_reconciliation < self.full_reconciliation_seconds:
            return
        with self.session_factory() as db:
            account_ids = AccountRepository.list_account_ids(db)
            # 每次全量恢复扫描同步一次数据库事实总量，避免逐订单事件维护
            # 内存计数在Worker重启或消息重放后失真。
            self.stats.liquidation_orders_filled = (
                RiskRepository.count_filled_liquidation_orders(db)
            )
        self.risk_store.mark_many_dirty(account_ids)
        self.last_reconciliation = now

    def _renew_lease_if_due(self) -> bool:
        """长批次中续租单写者租约；续租失败后立即停止本轮处理。"""

        now = time.monotonic()
        if now - self.last_lease_renewal < self.lease_renew_seconds:
            return True
        renewed = self.risk_store.renew_lease(self.owner, self.lease_ttl_ms)
        if renewed:
            self.last_lease_renewal = now
        else:
            logger.warning("风险Worker租约已丢失 owner=%s", self.owner)
        return renewed

    def run_once(self) -> None:
        if not self.risk_store.acquire_lease(self.owner, self.lease_ttl_ms):
            return
        started = time.monotonic()
        self.last_lease_renewal = started
        try:
            self._recover_all_accounts_if_due()
            dirty = self.risk_store.list_dirty(self.batch_size)
            for account_id, version in dirty:
                if not self._renew_lease_if_due():
                    break
                try:
                    result = self.monitor_service.process_account(account_id)
                    self.stats.accounts_checked += 1
                    self.stats.open_orders_cancelled += result.open_orders_cancelled
                    self.stats.accounts_warning += int(result.state == "WARNING")
                    self.stats.accounts_margin_deficit += int(
                        result.state in {"MARGIN_DEFICIT", "LIQUIDATION_PENDING", "LIQUIDATING"}
                    )
                    self.stats.accounts_recovered += int(result.state == "RECOVERED")
                    self.stats.valuation_unavailable += int(
                        result.state == "VALUATION_UNAVAILABLE"
                    )
                    self.stats.liquidation_tasks_created += int(
                        result.liquidation_task_id is not None
                    )
                    if result.retain_dirty:
                        self.stats.dirty_retained += 1
                    else:
                        self.risk_store.complete_dirty(account_id, version)
                except Exception:
                    self.stats.worker_failures += 1
                    logger.exception("账户风险复核失败 account_id=%s", account_id)

            actions = 0
            with self.session_factory() as db:
                tasks = list(
                    RiskRepository.list_recoverable_tasks(
                        db, limit=self.max_actions_per_cycle
                    )
                )
                task_ids = [task.task_id for task in tasks]
                db.commit()
            for task_id in task_ids:
                if actions >= self.max_actions_per_cycle:
                    break
                if not self._renew_lease_if_due():
                    break
                action = self.liquidation_service.execute_task(task_id)
                if action == "ORDER_CREATED":
                    self.stats.liquidation_orders_created += 1
                    actions += 1
        finally:
            self.stats.calculation_duration_ms = int(
                (time.monotonic() - started) * 1000
            )
            self.risk_store.release_lease(self.owner)

    def run_forever(self) -> None:
        logger.info("统一账户风险Worker已启动 owner=%s", self.owner)
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                self.stats.worker_failures += 1
                logger.exception("风险监控循环异常")
            self.stop_event.wait(self.interval_seconds)


def build_worker() -> RiskMonitorWorker:
    store = RiskStore(redis_client)
    market_tick_store = MarketTickStore(redis_client)
    monitor = RiskMonitorService(
        session_factory=SessionLocal,
        cancellation_service=get_order_cancellation_service(),
        revaluation_service=PnlSnapshotPersistenceService(
            session_factory=SessionLocal,
            pnl_store=RealtimePnlStore(redis_client),
            market_tick_store=market_tick_store,
            risk_store=store,
        ),
    )
    liquidation = LiquidationService(
        session_factory=SessionLocal,
        order_service=get_order_service(),
        cancellation_service=get_order_cancellation_service(),
        market_tick_store=market_tick_store,
        max_retries=settings.risk_liquidation_max_retries,
    )
    return RiskMonitorWorker(
        risk_store=store,
        monitor_service=monitor,
        liquidation_service=liquidation,
        interval_ms=settings.risk_monitor_interval_ms,
        batch_size=settings.risk_monitor_batch_size,
        max_actions_per_cycle=settings.risk_max_actions_per_cycle,
        lease_ttl_seconds=settings.risk_worker_lease_ttl_seconds,
        lease_renew_seconds=settings.risk_worker_lease_renew_seconds,
        full_reconciliation_seconds=settings.risk_full_reconciliation_interval_seconds,
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
