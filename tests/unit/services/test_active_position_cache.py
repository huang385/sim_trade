from types import SimpleNamespace

import pytest

from app.services.active_position_cache import ActivePositionCache


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class EmptyPositionRepository:
    def __init__(self):
        self.calls = 0

    def list_active_calculation_rows(self, _db):
        self.calls += 1
        return []


class EmptyAccountRepository:
    def list_by_account_ids(self, _db, _account_ids):
        return []


def test_cycle_snapshot_reads_version_once_and_is_immutable():
    versions = iter(["1", "1", "2"])
    version_calls = []

    def load_version():
        version_calls.append(1)
        return next(versions)

    repository = EmptyPositionRepository()
    cache = ActivePositionCache(
        session_factory=FakeSession,
        position_repository=repository,
        account_repository=EmptyAccountRepository(),
        refresh_ms=60_000,
        version_loader=load_version,
    )

    first = cache.get_cycle_snapshot()
    # 同一周期的所有查询都在不可变对象上完成，不再检查Redis版本。
    first.get_by_contract("SHFE", "RB2610")
    first.get_by_account("A001")
    first.get_account("A001")
    assert len(version_calls) == 1
    assert repository.calls == 1
    with pytest.raises(TypeError):
        first.by_account["A001"] = ()

    cache.get_cycle_snapshot()
    assert repository.calls == 1
    cache.get_cycle_snapshot()
    assert repository.calls == 2
