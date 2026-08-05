from unittest.mock import Mock

from app.infrastructure.risk_store import RiskStore


def test_mark_dirty_uses_atomic_lua_and_returns_version():
    redis = Mock()
    redis.eval.return_value = "3"
    assert RiskStore(redis).mark_dirty("A001") == "3"
    assert redis.eval.call_args.args[-1] == "A001"


def test_mark_dirty_once_uses_independent_idempotency_key():
    redis = Mock()
    redis.eval.return_value = "4"
    result = RiskStore(redis).mark_dirty_once(account_id="A001", event_id="E1")
    assert result == "4"
    assert "risk:processed_trigger:E1" in redis.eval.call_args.args


def test_dirty_completion_is_cas_and_does_not_clear_new_version():
    redis = Mock()
    redis.eval.return_value = 0
    assert RiskStore(redis).complete_dirty("A001", "old") is False
    assert redis.eval.call_args.args[-2:] == ("A001", "old")


def test_dirty_scan_persists_cursor_and_returns_concrete_versions():
    redis = Mock()
    redis.get.return_value = "7"
    redis.sscan.return_value = (11, ["A2", "A1"])
    redis.hmget.return_value = ["9", "10"]
    result = RiskStore(redis).list_dirty(2)
    assert result == [("A2", "9"), ("A1", "10")]
    redis.set.assert_called_once_with("risk:dirty_scan_cursor", "11")


def test_lease_is_owner_checked_for_renew_and_release():
    redis = Mock()
    redis.set.return_value = True
    redis.eval.side_effect = [1, 1]
    store = RiskStore(redis)
    assert store.acquire_lease("W1", 1000)
    assert store.renew_lease("W1", 1000)
    assert store.release_lease("W1")
