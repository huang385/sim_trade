from types import SimpleNamespace
from decimal import Decimal

import pytest

from app.services.active_position_cache import ActivePositionCache


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TrackingPostgresSession(FakeSession):
    """记录一致性事务配置和查询顺序的PostgreSQL Session替身。"""

    def __init__(self, events):
        self.events = events
        self.dialect = SimpleNamespace(name="postgresql")

    def get_bind(self):
        self.events.append("get_bind")
        return SimpleNamespace(dialect=self.dialect)

    def connection(self, *, execution_options):
        self.events.append(("connection", execution_options))
        return self

    def execute(self, statement):
        self.events.append(("execute", str(statement)))


class EmptyPositionRepository:
    def __init__(self):
        self.calls = 0
        self.contract_calls = 0
        self.active_rows = []
        self.contract_rows = []

    def list_active_calculation_rows(self, _db):
        self.calls += 1
        return list(self.active_rows)

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


class EmptyOutboxRepository:
    def __init__(self, versions=None):
        self.versions = versions or {}

    def list_latest_fact_versions(self, _db, **_kwargs):
        return dict(self.versions)


class TrackingPositionRepository(EmptyPositionRepository):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def list_active_calculation_rows(self, db):
        self.events.append(("full_rows", id(db)))
        return super().list_active_calculation_rows(db)

    def list_active_calculation_rows_by_contracts(self, db, contract_keys):
        self.events.append(("contract_rows", id(db)))
        return super().list_active_calculation_rows_by_contracts(
            db, contract_keys
        )


class TrackingAccountRepository(EmptyAccountRepository):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def list_by_account_ids(self, db, account_ids):
        self.events.append(("accounts", id(db)))
        return super().list_by_account_ids(db, account_ids)


class TrackingOutboxRepository(EmptyOutboxRepository):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def list_latest_fact_versions(self, db, **kwargs):
        self.events.append(("outbox", id(db)))
        return super().list_latest_fact_versions(db, **kwargs)


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
        multiplier_snapshot=Decimal("10"),
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
        outbox_repository=EmptyOutboxRepository(),
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
        outbox_repository=EmptyOutboxRepository(),
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
    refreshed_again = cache.get_cycle_snapshot(
        extra_account_ids={"A001"},
        refresh_account_versions={"A001": "8"},
    )

    assert positions.calls == 1
    assert positions.contract_calls == 0
    assert accounts.calls == 3
    assert refreshed.get_account("A001").frozen_margin == Decimal("5000")
    assert refreshed_again.get_account("A001").frozen_margin == Decimal("5000")

    # 模拟Worker重启：新缓存没有进程内版本记录，Redis中仍存在的版本8必须
    # 让它重新从PostgreSQL加载账户基础字段。
    restarted_cache = ActivePositionCache(
        session_factory=FakeSession,
        position_repository=positions,
        account_repository=accounts,
        outbox_repository=EmptyOutboxRepository(),
        refresh_ms=60_000,
        version_loader=lambda: "1",
    )
    restarted_cache.get_cycle_snapshot(
        extra_account_ids={"A001"},
        refresh_account_versions={"A001": "8"},
    )
    assert accounts.calls == 4


def test_contract_fact_refresh_adds_and_removes_only_target_contract():
    positions = EmptyPositionRepository()
    accounts = EmptyAccountRepository()
    account = make_account()
    accounts.accounts["A001"] = account
    cache = ActivePositionCache(
        session_factory=FakeSession,
        position_repository=positions,
        account_repository=accounts,
        outbox_repository=EmptyOutboxRepository(),
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
        multiplier_snapshot=Decimal("10"),
    )
    detail = SimpleNamespace(
        position_detail_id="PD1",
        open_price=Decimal("3200"),
        pnl_base_price=Decimal("3200"),
        remaining_volume=1,
        multiplier_snapshot=Decimal("10"),
    )
    instrument = SimpleNamespace(contract_multiplier=Decimal("99"))
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
    assert added.get_by_contract("DCE", "JD2609")[0].contract_multiplier == Decimal(
        "10"
    )
    assert len(added.get_by_contract("DCE", "JD2609")) == 1
    assert removed.get_by_contract("DCE", "JD2609") == ()
    assert removed.get_by_account("A001") == ()


def test_cycle_snapshot_carries_postgres_outbox_fact_versions():
    positions = EmptyPositionRepository()
    accounts = EmptyAccountRepository()
    account = make_account()
    accounts.accounts["A001"] = account
    position = SimpleNamespace(
        position_id="P1",
        account_id="A001",
        order_book_id="JD2609",
        exchange_id="DCE",
        symbol="JD2609",
        direction="LONG",
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        multiplier_snapshot=Decimal("10"),
    )
    detail = SimpleNamespace(
        position_detail_id="PD1",
        open_price=Decimal("3200"),
        pnl_base_price=Decimal("3200"),
        remaining_volume=1,
        multiplier_snapshot=Decimal("10"),
    )
    instrument = SimpleNamespace(contract_multiplier=Decimal("10"))
    positions.active_rows = [(position, detail, instrument, account)]
    outbox = EmptyOutboxRepository(
        {
            ("ACCOUNT", "A001"): "70",
            ("POSITION", "P1"): "80",
        }
    )
    cache = ActivePositionCache(
        session_factory=FakeSession,
        position_repository=positions,
        account_repository=accounts,
        outbox_repository=outbox,
        refresh_ms=60_000,
        version_loader=lambda: "1",
    )

    cycle = cache.get_cycle_snapshot(
        extra_account_ids={"A001"},
        refresh_account_versions={"A001": "1"},
        refresh_contract_versions={("DCE", "JD2609"): "1"},
    )

    assert cycle.get_account("A001").source_fact_version == "70"
    assert cycle.get_by_contract("DCE", "JD2609")[0].source_fact_version == (
        "80"
    )


def test_full_reload_uses_one_repeatable_read_only_transaction():
    events = []
    session = TrackingPostgresSession(events)
    positions = TrackingPositionRepository(events)
    accounts = TrackingAccountRepository(events)
    outbox = TrackingOutboxRepository(events)
    cache = ActivePositionCache(
        session_factory=lambda: session,
        position_repository=positions,
        account_repository=accounts,
        outbox_repository=outbox,
        refresh_ms=60_000,
    )

    cache.get_cycle_snapshot(extra_account_ids={"A001"})

    assert events[:3] == [
        "get_bind",
        ("connection", {"isolation_level": "REPEATABLE READ"}),
        ("execute", "SET TRANSACTION READ ONLY"),
    ]
    query_events = [item for item in events if isinstance(item, tuple)][2:]
    assert [item[0] for item in query_events] == [
        "full_rows",
        "accounts",
        "outbox",
    ]
    assert {item[1] for item in query_events} == {id(session)}


def test_incremental_refresh_uses_one_repeatable_read_only_transaction():
    events = []
    sessions = []

    def session_factory():
        session = TrackingPostgresSession(events)
        sessions.append(session)
        return session

    cache = ActivePositionCache(
        session_factory=session_factory,
        position_repository=TrackingPositionRepository(events),
        account_repository=TrackingAccountRepository(events),
        outbox_repository=TrackingOutboxRepository(events),
        refresh_ms=60_000,
    )
    cache.get_cycle_snapshot()
    events.clear()

    cache.get_cycle_snapshot(
        refresh_account_versions={"A001": "2"},
        refresh_contract_versions={("DCE", "JD2609"): "2"},
    )

    assert events[:3] == [
        "get_bind",
        ("connection", {"isolation_level": "REPEATABLE READ"}),
        ("execute", "SET TRANSACTION READ ONLY"),
    ]
    query_events = [item for item in events if isinstance(item, tuple)][2:]
    assert [item[0] for item in query_events] == [
        "contract_rows",
        "accounts",
        "outbox",
    ]
    assert {item[1] for item in query_events} == {id(sessions[-1])}
