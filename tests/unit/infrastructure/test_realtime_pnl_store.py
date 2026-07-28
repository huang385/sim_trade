from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.schemas.pnl_schema import PositionRealtimePnl


def test_trade_dirty_is_written_by_one_atomic_lua_command():
    redis_client = Mock()
    redis_client.eval.return_value = "12"
    store = RealtimePnlStore(redis_client)

    version = store.mark_contract_dirty(
        exchange_id="shfe",
        symbol="rb2610",
        account_id="A001",
    )

    assert version == "12"
    redis_client.eval.assert_called_once()
    args = redis_client.eval.call_args.args
    assert args[1] == 4
    assert "pnl:dirty_contracts" in args
    assert "pnl:dirty_contract_versions" in args


def test_tick_snapshot_write_does_not_repeat_static_indexes():
    redis_client = Mock()
    pipeline = redis_client.pipeline.return_value
    pipeline.execute.return_value = []
    store = RealtimePnlStore(redis_client)
    now = datetime.now(timezone.utc)
    position = PositionRealtimePnl(
        position_id="P001",
        account_id="A001",
        exchange_id="SHFE",
        symbol="RB2610",
        direction="LONG",
        mark_price=Decimal("3500"),
        cumulative_unrealized_pnl=Decimal("100"),
        daily_position_pnl=Decimal("100"),
        event_time=now,
        source_event_id="T1",
        updated_at=now,
    )

    store.write_snapshots(
        positions=[position],
        accounts=[],
        dirty_version="1-0",
    )

    calls = [str(call) for call in pipeline.method_calls]
    assert not any("pnl:account_positions:" in call for call in calls)
    assert not any("pnl:contract_positions:" in call for call in calls)


def test_dirty_contract_cas_does_not_delete_newer_version():
    redis_client = Mock()
    redis_client.eval.return_value = 0
    store = RealtimePnlStore(redis_client)

    completed = store.complete_dirty_contract(
        exchange_id="SHFE",
        symbol="RB2610",
        expected_version="11",
    )

    assert completed is False
    redis_client.delete.assert_not_called()


def test_worker_lease_release_is_owner_checked_by_lua():
    redis_client = Mock()
    redis_client.eval.return_value = 1
    store = RealtimePnlStore(redis_client)

    assert store.release_worker_lease("worker-1") is True
    args = redis_client.eval.call_args.args
    assert args[-1] == "worker-1"


def test_list_active_contract_codes_filters_empty_indexes_and_batches_scard():
    redis_client = Mock()
    redis_client.smembers.return_value = {
        "pnl:contract_positions:DCE:JD2609",
        "pnl:contract_positions:SHFE:AG2612",
        "pnl:contract_positions:DCE:CLOSED2609",
    }
    pipeline = redis_client.pipeline.return_value
    # index_keys按字符串排序：CLOSED2609、JD2609、AG2612。
    pipeline.execute.return_value = [0, 2, 1]
    store = RealtimePnlStore(redis_client)

    result = store.list_active_contract_codes()

    assert result == {"JD2609", "AG2612"}
    assert pipeline.scard.call_count == 3
    # 读取路径只过滤空集合，不删除索引，避免并发新建持仓时误删新索引。
    redis_client.srem.assert_not_called()
