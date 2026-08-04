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
    store.get_accounts_with_positions.side_effect = RedisError("offline")

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
    store.get_accounts_with_positions.return_value = ({"A001": {}}, {"P001": {}})

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
    store.get_accounts_with_positions.side_effect = RedisError("stop")

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
