from types import SimpleNamespace

from app.workers.pnl_snapshot_persistence_worker import (
    PnlSnapshotPersistenceWorker,
)


class FakePersistenceService:
    def __init__(self):
        self.requested = [500, 500, 200, 0]
        self.calls = 0

    def persist_batch(self, batch_size):
        assert batch_size == 500
        requested = self.requested[self.calls]
        self.calls += 1
        return SimpleNamespace(
            requested=requested,
            positions_persisted=requested,
            accounts_persisted=1 if requested else 0,
            retained=0,
        )


def test_more_than_one_batch_is_drained_in_same_cycle():
    service = FakePersistenceService()
    worker = PnlSnapshotPersistenceWorker(
        service=service,
        interval_ms=1000,
        batch_size=500,
        max_batches_per_cycle=10,
        time_budget_ms=1000,
        monotonic=lambda: 0,
    )

    worker.run_once()

    assert service.calls == 4
    assert worker.stats.postgres_positions_persisted == 1200
