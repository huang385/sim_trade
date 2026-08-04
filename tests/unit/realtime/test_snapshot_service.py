from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from redis.exceptions import RedisError

from app.realtime.snapshot_service import SnapshotService
from app.realtime.subscription_service import RealtimeUserIdentity


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


def test_strict_websocket_snapshot_rejects_redis_read_failure():
    db = _session_with_active_position()
    store = Mock()
    store.get_accounts_with_positions_and_versions.side_effect = RedisError(
        "offline"
    )

    with pytest.raises(RedisError):
        SnapshotService(store).build(
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
    )

    with pytest.raises(RedisError, match="Hash缺失"):
        SnapshotService(store).build(
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
        SnapshotService(store).build(
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
    )

    with pytest.raises(RedisError, match="版本不一致"):
        SnapshotService(store).build(
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
    )

    with pytest.raises(RedisError, match="Hash不完整"):
        SnapshotService(store).build(
            db,
            {"A001"},
            identity=RealtimeUserIdentity("U001", "USER"),
            require_realtime_consistency=True,
        )
