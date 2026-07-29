from types import SimpleNamespace
from decimal import Decimal

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
        self.contract_calls = 0
        self.contract_rows = []

    def list_active_calculation_rows(self, _db):
        self.calls += 1
        return []

    def list_active_calculation_rows_by_contracts(
        self,
        _db,
        _contract_keys,
    ):
        self.contract_calls += 1
        return list(self.contract_rows)


class EmptyAccountRepository:
    def __init__(self):
        self.calls = 0
        self.accounts = {}

    def list_by_account_ids(self, _db, _account_ids):
        self.calls += 1
        return [
            self.accounts[account_id]
            for account_id in _account_ids
            if account_id in self.accounts
        ]


def make_account(account_id="A001", frozen_margin="0"):
    return SimpleNamespace(
        account_id=account_id,
        cash_balance=Decimal("100000"),
        used_margin=Decimal("10000"),
        frozen_margin=Decimal(frozen_margin),
        frozen_cash=Decimal("0"),
        frozen_commission=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        daily_close_pnl=Decimal("0"),
        daily_commission=Decimal("0"),
    )


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


def test_account_fact_version_refreshes_only_accounts_once():
    positions = EmptyPositionRepository()
    accounts = EmptyAccountRepository()
    accounts.accounts["A001"] = make_account()
    cache = ActivePositionCache(
        session_factory=FakeSession,
        position_repository=positions,
        account_repository=accounts,
        refresh_ms=60_000,
        version_loader=lambda: "1",
    )
    cache.get_cycle_snapshot(extra_account_ids={"A001"})

    accounts.accounts["A001"] = make_account(frozen_margin="5000")
    refreshed = cache.get_cycle_snapshot(
        extra_account_ids={"A001"},
        refresh_account_versions={"A001": "7"},
    )
    cache.get_cycle_snapshot(
        extra_account_ids={"A001"},
        refresh_account_versions={"A001": "7"},
    )

    assert positions.calls == 1
    assert positions.contract_calls == 0
    assert accounts.calls == 2
    assert refreshed.get_account("A001").frozen_margin == Decimal("5000")


def test_contract_fact_refresh_adds_and_removes_only_target_contract():
    positions = EmptyPositionRepository()
    accounts = EmptyAccountRepository()
    account = make_account()
    accounts.accounts["A001"] = account
    cache = ActivePositionCache(
        session_factory=FakeSession,
        position_repository=positions,
        account_repository=accounts,
        refresh_ms=60_000,
        version_loader=lambda: "1",
    )
    cache.get_cycle_snapshot()

    position = SimpleNamespace(
        position_id="P1",
        account_id="A001",
        order_book_id="JD2609",
        exchange_id="DCE",
        symbol="JD2609",
        direction="LONG",
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
    )
    detail = SimpleNamespace(
        position_detail_id="PD1",
        open_price=Decimal("3200"),
        pnl_base_price=Decimal("3200"),
        remaining_volume=1,
    )
    instrument = SimpleNamespace(contract_multiplier=Decimal("10"))
    positions.contract_rows = [
        (position, detail, instrument, account)
    ]
    added = cache.get_cycle_snapshot(
        extra_account_ids={"A001"},
        refresh_account_versions={"A001": "trade:7"},
        refresh_contract_versions={("DCE", "JD2609"): "7"},
    )

    positions.contract_rows = []
    removed = cache.get_cycle_snapshot(
        extra_account_ids={"A001"},
        refresh_account_versions={"A001": "trade:8"},
        refresh_contract_versions={("DCE", "JD2609"): "8"},
    )

    assert positions.calls == 1
    assert positions.contract_calls == 2
    assert len(added.get_by_contract("DCE", "JD2609")) == 1
    assert removed.get_by_contract("DCE", "JD2609") == ()
    assert removed.get_by_account("A001") == ()
