from types import SimpleNamespace
from unittest.mock import Mock

from app.workers.risk_monitor_worker import RiskMonitorWorker


class SessionContext:
    def __init__(self):
        self.db = Mock()
        self.db.scalars.return_value.all.return_value = []

    def __call__(self):
        context = Mock()
        context.__enter__ = Mock(return_value=self.db)
        context.__exit__ = Mock(return_value=False)
        return context


def build_worker(result):
    store = Mock()
    store.acquire_lease.return_value = True
    store.list_dirty.return_value = [("A001", "3")]
    monitor = Mock()
    monitor.process_account.return_value = result
    liquidation = Mock()
    return (
        RiskMonitorWorker(
            risk_store=store,
            monitor_service=monitor,
            liquidation_service=liquidation,
            session_factory=SessionContext(),
            full_reconciliation_seconds=10**12,
        ),
        store,
    )


def test_successful_cycle_clears_only_expected_dirty_version():
    worker, store = build_worker(
        SimpleNamespace(
            state="NORMAL",
            open_orders_cancelled=0,
            liquidation_task_id=None,
            retain_dirty=False,
        )
    )
    worker.last_reconciliation = 1
    worker.run_once()
    store.complete_dirty.assert_called_once_with("A001", "3")


def test_version_change_or_unavailable_valuation_retains_dirty():
    worker, store = build_worker(
        SimpleNamespace(
            state="VALUATION_UNAVAILABLE",
            open_orders_cancelled=0,
            liquidation_task_id=None,
            retain_dirty=True,
        )
    )
    worker.last_reconciliation = 1
    worker.run_once()
    store.complete_dirty.assert_not_called()


def test_failure_retains_dirty_and_releases_lease():
    worker, store = build_worker(None)
    worker.monitor_service.process_account.side_effect = RuntimeError("db down")
    worker.last_reconciliation = 1
    worker.run_once()
    store.complete_dirty.assert_not_called()
    store.release_lease.assert_called_once_with(worker.owner)
    assert worker.stats.worker_failures == 1


def test_lost_lease_stops_processing_remaining_dirty_accounts():
    worker, store = build_worker(
        SimpleNamespace(
            state="NORMAL",
            open_orders_cancelled=0,
            liquidation_task_id=None,
            retain_dirty=False,
        )
    )
    store.list_dirty.return_value = [("A001", "1"), ("A002", "1")]
    store.renew_lease.return_value = False
    worker.lease_renew_seconds = 1
    worker.last_reconciliation = 1
    result = worker.monitor_service.process_account.return_value

    def process_first_account(account_id):
        assert account_id == "A001"
        # 模拟首个账户处理耗时已经超过续租间隔。
        worker.last_lease_renewal = 0
        return result

    worker.monitor_service.process_account.side_effect = process_first_account
    worker.run_once()
    worker.monitor_service.process_account.assert_called_once_with("A001")


def test_restart_cycle_recovers_persisted_liquidation_task():
    worker, store = build_worker(
        SimpleNamespace(
            state="NORMAL",
            open_orders_cancelled=0,
            liquidation_task_id=None,
            retain_dirty=False,
        )
    )
    store.list_dirty.return_value = []
    worker.session_factory.db.scalars.return_value.all.return_value = [
        SimpleNamespace(task_id="T-PENDING")
    ]
    worker.liquidation_service.execute_task.return_value = "ORDER_CREATED"
    worker.last_reconciliation = 1

    worker.run_once()

    worker.liquidation_service.execute_task.assert_called_once_with("T-PENDING")
    assert worker.stats.liquidation_orders_created == 1
