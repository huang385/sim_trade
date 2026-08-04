from types import SimpleNamespace
from unittest.mock import Mock
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from redis.exceptions import RedisError

from app.realtime.snapshot_service import SnapshotService
from app.realtime.subscription_service import RealtimeUserIdentity


def _snapshot_service(
    store: Mock,
    *,
    account_fact_version: str = "7",
    position_fact_version: str = "8",
) -> SnapshotService:
    outbox_repository = Mock()
    outbox_repository.list_latest_fact_versions.return_value = {
        ("ACCOUNT", "A001"): account_fact_version,
        ("POSITION", "P001"): position_fact_version,
    }
    return SnapshotService(store, outbox_repository=outbox_repository)


def _session_with_active_position(*, dialect: str = "sqlite") -> Mock:
    db = Mock()
    db.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name=dialect)
    )
    account = SimpleNamespace(id=1, account_id="A001", user_id="U001")
    position = SimpleNamespace(
        id=1,
        position_id="P001",
        account_id="A001",
    )
    db.scalars.side_effect = [
        SimpleNamespace(all=lambda: [account]),
        SimpleNamespace(all=lambda: [position]),
        SimpleNamespace(all=lambda: []),
        SimpleNamespace(all=lambda: []),
        SimpleNamespace(all=lambda: []),
    ]
    return db


def _account_without_positions() -> SimpleNamespace:
    now = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=1,
        account_id="A001",
        user_id="U001",
        account_name="零持仓账户",
        account_type="FUTURES",
        option_trading_enabled=True,
        initial_cash=Decimal("100000"),
        cash_balance=Decimal("100000"),
        available_cash=Decimal("98000"),
        frozen_cash=Decimal("500"),
        equity=Decimal("100099"),
        used_margin=Decimal("0"),
        frozen_margin=Decimal("1000"),
        option_used_margin=Decimal("0"),
        option_realtime_required_margin=Decimal("0"),
        long_option_market_value=Decimal("0"),
        short_option_market_value=Decimal("0"),
        net_option_market_value=Decimal("0"),
        risk_available_cash=Decimal("98000"),
        risk_state="NORMAL",
        realized_pnl=Decimal("50"),
        unrealized_pnl=Decimal("99"),
        daily_position_pnl=Decimal("99"),
        daily_close_pnl=Decimal("50"),
        daily_commission=Decimal("5"),
        daily_pnl=Decimal("144"),
        used_commission=Decimal("5"),
        frozen_commission=Decimal("200"),
        risk_ratio=Decimal("0"),
        status="NORMAL",
        trading_day=date(2026, 8, 4),
        created_at=now,
        updated_at=now,
    )


def _session_without_positions() -> Mock:
    db = Mock()
    db.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name="sqlite")
    )
    db.scalars.side_effect = [
        SimpleNamespace(all=lambda: [_account_without_positions()]),
        SimpleNamespace(all=lambda: []),
        SimpleNamespace(all=lambda: []),
        SimpleNamespace(all=lambda: []),
    ]
    return db


def test_strict_websocket_snapshot_rejects_redis_read_failure():
    db = _session_with_active_position()
    store = Mock()
    store.get_accounts_with_positions_and_versions.side_effect = RedisError(
        "offline"
    )

    with pytest.raises(RedisError):
        _snapshot_service(store).build(
            db,
            {"A001"},
            identity=RealtimeUserIdentity("U001", "USER"),
            require_realtime_consistency=True,
        )


def test_strict_snapshot_rejects_missing_realtime_hashes():
    db = _session_with_active_position()
    store = Mock()
    store.get_accounts_with_positions_and_versions.return_value = (
        {"A001": {}},
        {"P001": {}},
        {"A001": "2"},
        {"P001": "2"},
        set(),
        set(),
    )

    with pytest.raises(RedisError, match="Hash缺失"):
        _snapshot_service(store).build(
            db,
            {"A001"},
            identity=RealtimeUserIdentity("U001", "USER"),
            require_realtime_consistency=True,
        )


def test_postgres_strict_snapshot_uses_read_only_repeatable_read():
    db = _session_with_active_position(dialect="postgresql")
    store = Mock()
    store.get_accounts_with_positions_and_versions.side_effect = RedisError(
        "stop"
    )

    with pytest.raises(RedisError):
        _snapshot_service(store).build(
            db,
            {"A001"},
            identity=RealtimeUserIdentity("U001", "USER"),
            require_realtime_consistency=True,
        )

    db.connection.assert_called_once_with(
        execution_options={"isolation_level": "REPEATABLE READ"}
    )
    assert "SET TRANSACTION READ ONLY" in str(db.execute.call_args.args[0])


def _complete_account_hash(version: str) -> dict[str, str]:
    return {
        "account_id": "A001",
        "cumulative_unrealized_pnl": "10.000000",
        "daily_position_pnl": "10.000000",
        "daily_close_pnl": "0.000000",
        "daily_commission": "1.000000",
        "daily_pnl": "9.000000",
        "equity": "100010.000000",
        "available_cash": "90000.000000",
        "risk_ratio": "0.10000000",
        "updated_at": "2026-08-04T12:00:00+00:00",
        "realtime_snapshot_version": version,
        "source_account_fact_version": "7",
    }


def _complete_position_hash(version: str) -> dict[str, str]:
    return {
        "position_id": "P001",
        "account_id": "A001",
        "exchange_id": "DCE",
        "symbol": "JD2609",
        "direction": "LONG",
        "mark_price": "3500.000000",
        "cumulative_unrealized_pnl": "10.000000",
        "daily_position_pnl": "10.000000",
        "event_time": "2026-08-04T12:00:00+00:00",
        "source_event_id": "TICK-1",
        "updated_at": "2026-08-04T12:00:00+00:00",
        "realtime_snapshot_version": version,
        "source_position_fact_version": "8",
    }


@pytest.mark.parametrize(
    ("account_hash_version", "account_latest", "position_hash_version", "position_latest"),
    [
        ("1", "2", "2", "2"),
        ("2", "2", "1", "2"),
        ("", "2", "2", "2"),
        ("2", "", "2", "2"),
    ],
)
def test_strict_snapshot_rejects_stale_or_unverifiable_hash_version(
    account_hash_version,
    account_latest,
    position_hash_version,
    position_latest,
):
    db = _session_with_active_position()
    store = Mock()
    store.get_accounts_with_positions_and_versions.return_value = (
        {"A001": _complete_account_hash(account_hash_version)},
        {"P001": _complete_position_hash(position_hash_version)},
        {"A001": account_latest},
        {"P001": position_latest},
        set(),
        set(),
    )

    with pytest.raises(RedisError, match="版本不一致"):
        _snapshot_service(store).build(
            db,
            {"A001"},
            identity=RealtimeUserIdentity("U001", "USER"),
            require_realtime_consistency=True,
        )


def test_strict_snapshot_rejects_complete_but_invalid_hash_fields():
    db = _session_with_active_position()
    store = Mock()
    invalid_account = _complete_account_hash("3")
    invalid_account["equity"] = "not-a-decimal"
    store.get_accounts_with_positions_and_versions.return_value = (
        {"A001": invalid_account},
        {"P001": _complete_position_hash("3")},
        {"A001": "3"},
        {"P001": "3"},
        set(),
        set(),
    )

    with pytest.raises(RedisError, match="Hash不完整"):
        _snapshot_service(store).build(
            db,
            {"A001"},
            identity=RealtimeUserIdentity("U001", "USER"),
            require_realtime_consistency=True,
        )


@pytest.mark.parametrize(
    ("account_source", "position_source", "account_current", "position_current"),
    [
        ("6", "8", "7", "8"),
        ("7", "7", "7", "8"),
    ],
)
def test_strict_snapshot_rejects_pnl_behind_postgres_fact(
    account_source,
    position_source,
    account_current,
    position_current,
):
    db = _session_with_active_position()
    store = Mock()
    account_hash = _complete_account_hash("9")
    account_hash["source_account_fact_version"] = account_source
    position_hash = _complete_position_hash("9")
    position_hash["source_position_fact_version"] = position_source
    store.get_accounts_with_positions_and_versions.return_value = (
        {"A001": account_hash},
        {"P001": position_hash},
        {"A001": "9"},
        {"P001": "9"},
        set(),
        set(),
    )

    with pytest.raises(RedisError, match="尚未覆盖最新业务事实"):
        _snapshot_service(
            store,
            account_fact_version=account_current,
            position_fact_version=position_current,
        ).build(
            db,
            {"A001"},
            identity=RealtimeUserIdentity("U001", "USER"),
            require_realtime_consistency=True,
        )


@pytest.mark.parametrize(
    ("dirty_facts", "dirty_structures"),
    [({"A001"}, set()), (set(), {"A001"})],
)
def test_strict_snapshot_rejects_related_unfinished_dirty(
    dirty_facts,
    dirty_structures,
):
    db = _session_with_active_position()
    store = Mock()
    store.get_accounts_with_positions_and_versions.return_value = (
        {"A001": _complete_account_hash("9")},
        {"P001": _complete_position_hash("9")},
        {"A001": "9"},
        {"P001": "9"},
        dirty_facts,
        dirty_structures,
    )

    with pytest.raises(RedisError, match="未处理业务事实"):
        _snapshot_service(store).build(
            db,
            {"A001"},
            identity=RealtimeUserIdentity("U001", "USER"),
            require_realtime_consistency=True,
        )


def test_strict_snapshot_ignores_stale_redis_pnl_after_all_positions_closed():
    db = _session_without_positions()
    store = Mock()
    stale = _complete_account_hash("4")
    stale.update(
        {
            "cumulative_unrealized_pnl": "99.000000",
            "daily_position_pnl": "99.000000",
            "equity": "100099.000000",
        }
    )
    store.get_accounts_with_positions.return_value = (
        {"A001": stale},
        {"P-CLOSED": _complete_position_hash("4")},
    )

    result = _snapshot_service(store).build(
        db,
        {"A001"},
        identity=RealtimeUserIdentity("U001", "USER"),
        require_realtime_consistency=True,
    )

    snapshot = result["accounts"][0]
    assert snapshot["positions"] == []
    assert snapshot["pnl"]["unrealized_pnl"] == "0.000000"
    assert snapshot["pnl"]["daily_position_pnl"] == "0.000000"
    assert snapshot["pnl"]["daily_pnl"] == "45.000000"
    assert snapshot["pnl"]["equity"] == "100000.000000"
    assert snapshot["pnl"]["available_cash"] == "98300.000000"
    assert snapshot["pnl"]["risk_ratio"] == "0E-8"
    assert snapshot["pnl"]["data_source"] == "POSTGRES_ZERO_POSITION"
    assert snapshot["valuation"]["data_source"] == (
        "POSTGRES_ZERO_POSITION"
    )
    store.get_accounts_with_positions.assert_not_called()
    store.get_accounts_with_positions_and_versions.assert_not_called()
