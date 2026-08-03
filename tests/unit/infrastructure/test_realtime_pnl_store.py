from datetime import datetime, timezone
from decimal import Decimal
import json
from unittest.mock import Mock

from redis.exceptions import WatchError

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


def test_dirty_account_cas_does_not_delete_newer_version():
    redis_client = Mock()
    redis_client.eval.return_value = 0
    store = RealtimePnlStore(redis_client)

    completed = store.complete_dirty_account("A001", "old-version")

    assert completed is False
    args = redis_client.eval.call_args.args
    assert "pnl:dirty_account_versions" in args
    assert "pnl:dirty_accounts" in args


def test_worker_lease_release_is_owner_checked_by_lua():
    redis_client = Mock()
    redis_client.eval.return_value = 1
    store = RealtimePnlStore(redis_client)

    assert store.release_worker_lease("worker-1") is True
    args = redis_client.eval.call_args.args
    assert args[-1] == "worker-1"


def test_lease_guarded_cycle_write_rejects_without_pipeline_write():
    redis_client = Mock()
    redis_client.eval.return_value = 0
    store = RealtimePnlStore(redis_client)

    accepted, positions, accounts = (
        store.write_cycle_snapshots_if_lease_owned(
            lease_owner="worker-old",
            positions=[],
            accounts=[],
            dirty_version="cycle-1",
            active_positions=[],
            closed_positions=[],
        )
    )

    assert (accepted, positions, accounts) == (False, 0, 0)
    redis_client.pipeline.assert_not_called()
    assert redis_client.eval.call_args.args[-2] == "worker-old"


def test_contract_position_ids_many_uses_one_pipeline():
    redis_client = Mock()
    pipeline = redis_client.pipeline.return_value
    pipeline.execute.return_value = [{"P2"}, {"P1"}]
    store = RealtimePnlStore(redis_client)

    result = store.list_contract_position_ids_many(
        [("SHFE", "RB2610"), ("dce", "jd2609")]
    )

    assert result == {
        ("DCE", "JD2609"): {"P2"},
        ("SHFE", "RB2610"): {"P1"},
    }
    redis_client.pipeline.assert_called_once_with(transaction=False)
    assert pipeline.smembers.call_count == 2


def test_account_with_positions_uses_one_pipeline():
    redis_client = Mock()
    pipeline = redis_client.pipeline.return_value
    pipeline.execute.return_value = [
        {"account_id": "A001"},
        {"position_id": "P001"},
        {"position_id": "P002"},
    ]
    store = RealtimePnlStore(redis_client)

    account, positions = store.get_account_with_positions(
        account_id="A001",
        position_ids=["P001", "P002"],
    )

    assert account == {"account_id": "A001"}
    assert positions == {
        "P001": {"position_id": "P001"},
        "P002": {"position_id": "P002"},
    }
    redis_client.pipeline.assert_called_once_with(transaction=False)
    assert pipeline.hgetall.call_count == 3
    pipeline.execute.assert_called_once()


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


def test_account_fact_dirty_uses_independent_version_and_cas_keys():
    redis_client = Mock()
    redis_client.eval.return_value = "3"
    store = RealtimePnlStore(redis_client)

    version = store.mark_account_fact_dirty_once(
        event_id="E-ACCOUNT-1",
        account_id="A001",
        processed_ttl_seconds=60,
    )

    assert version == "3"
    args = redis_client.eval.call_args.args
    assert "pnl:dirty_account_facts" in args
    assert "pnl:dirty_account_fact_versions" in args
    assert "pnl:position_cache_version" not in args

    redis_client.eval.reset_mock()
    redis_client.eval.return_value = 0
    assert store.complete_dirty_account_fact("A001", "3") is False
    clear_args = redis_client.eval.call_args.args
    assert "pnl:dirty_account_fact_versions" in clear_args
    assert "pnl:dirty_account_facts" in clear_args
    # 专用CAS只SREM Dirty成员，不允许删除永久版本字段。
    assert "HDEL" not in clear_args[0]


def test_closed_position_prunes_empty_account_and_contract_meta_indexes():
    redis_client = Mock()
    redis_client.eval.return_value = 1
    store = RealtimePnlStore(redis_client)

    accepted, positions, accounts = (
        store.write_cycle_snapshots_if_lease_owned(
            lease_owner="worker-1",
            positions=[],
            accounts=[],
            dirty_version="cycle-1",
            active_positions=[],
            closed_positions=[
                ("A001", "DCE", "JD2609", "P001")
            ],
        )
    )

    assert (accepted, positions, accounts) == (True, 0, 0)
    operations = json.loads(redis_client.eval.call_args.args[-1])
    removals = [
        operation
        for operation in operations
        if operation[0] == "SREM_MEMBER_AND_PRUNE_INDEX"
    ]
    assert removals == [
        [
            "SREM_MEMBER_AND_PRUNE_INDEX",
            "pnl:account_positions:A001",
            "P001",
            "pnl:index_keys:accounts",
        ],
        [
            "SREM_MEMBER_AND_PRUNE_INDEX",
            "pnl:contract_positions:DCE:JD2609",
            "P001",
            "pnl:index_keys:contracts",
        ],
    ]


def test_dirty_position_scan_cursor_rotates_past_unprocessable_first_batch():
    redis_client = Mock()
    redis_client.eval.return_value = []
    # 第一次从0读取到坏数据并推进到游标9；下一次从9继续读取正常数据。
    redis_client.get.side_effect = ["0", "9"]
    redis_client.sscan.side_effect = [
        (9, ["P-BAD"]),
        (0, ["P-GOOD"]),
    ]
    redis_client.hmget.side_effect = [["v-bad"], ["v-good"]]
    store = RealtimePnlStore(redis_client)

    assert store.list_dirty_positions(1) == [("P-BAD", "v-bad")]
    assert store.list_dirty_positions(1) == [("P-GOOD", "v-good")]

    assert redis_client.sscan.call_args_list[0].kwargs["cursor"] == 0
    assert redis_client.sscan.call_args_list[1].kwargs["cursor"] == 9
    assert redis_client.set.call_args_list[0].args[-1] == "9"
    assert redis_client.set.call_args_list[1].args[-1] == "0"


def test_dirty_position_scan_cursor_is_reused_after_store_restart():
    redis_client = Mock()
    redis_client.eval.return_value = []
    redis_client.get.return_value = "27"
    redis_client.sscan.return_value = (0, ["P-AFTER-RESTART"])
    redis_client.hmget.return_value = ["v1"]

    # 新实例没有任何进程内游标，仍从Redis保存的27继续读取。
    result = RealtimePnlStore(redis_client).list_dirty_positions(10)

    assert result == [("P-AFTER-RESTART", "v1")]
    assert redis_client.sscan.call_args.kwargs["cursor"] == 27


def test_dirty_account_scan_cursor_rotates_past_unprocessable_first_batch():
    redis_client = Mock()
    redis_client.eval.return_value = []
    redis_client.get.side_effect = ["0", "15"]
    redis_client.sscan.side_effect = [
        (15, ["A-BAD"]),
        (0, ["A-GOOD"]),
    ]
    redis_client.hmget.side_effect = [["v-bad"], ["v-good"]]
    store = RealtimePnlStore(redis_client)

    assert store.list_dirty_accounts(1) == [("A-BAD", "v-bad")]
    assert store.list_dirty_accounts(1) == [("A-GOOD", "v-good")]

    assert redis_client.sscan.call_args_list[0].kwargs["cursor"] == 0
    assert redis_client.sscan.call_args_list[1].kwargs["cursor"] == 15
    assert redis_client.set.call_args_list[0].args[-1] == "15"
    assert redis_client.set.call_args_list[1].args[-1] == "0"


def test_rebuild_active_indexes_retries_watch_conflict_then_succeeds():
    redis_client = Mock()
    first_pipeline = Mock()
    first_pipeline.get.return_value = "3"
    first_pipeline.smembers.return_value = set()
    first_pipeline.execute.side_effect = WatchError("conflict")
    second_pipeline = Mock()
    second_pipeline.get.return_value = "3"
    second_pipeline.smembers.return_value = set()
    second_pipeline.execute.return_value = []
    redis_client.pipeline.side_effect = [
        first_pipeline,
        second_pipeline,
    ]
    store = RealtimePnlStore(redis_client)

    rebuilt = store.rebuild_active_indexes(
        expected_cache_version="3",
        positions=[("A001", "DCE", "JD2609", "P001")],
    )

    assert rebuilt is True
    first_pipeline.reset.assert_called_once()
    second_pipeline.reset.assert_called_once()


def test_rebuild_active_indexes_returns_false_after_watch_retry_limit():
    redis_client = Mock()
    pipelines = []
    for _ in range(3):
        pipeline = Mock()
        pipeline.get.return_value = "8"
        pipeline.smembers.return_value = set()
        pipeline.execute.side_effect = WatchError("conflict")
        pipelines.append(pipeline)
    redis_client.pipeline.side_effect = pipelines
    store = RealtimePnlStore(redis_client)

    rebuilt = store.rebuild_active_indexes(
        expected_cache_version="8",
        positions=[],
    )

    assert rebuilt is False
    assert all(
        pipeline.reset.call_count == 1 for pipeline in pipelines
    )


def test_rebuild_active_indexes_does_not_overwrite_newer_version():
    redis_client = Mock()
    pipeline = redis_client.pipeline.return_value
    pipeline.get.return_value = "12"
    store = RealtimePnlStore(redis_client)

    rebuilt = store.rebuild_active_indexes(
        expected_cache_version="11",
        positions=[("A001", "DCE", "JD2609", "P001")],
    )

    assert rebuilt is False
    pipeline.unwatch.assert_called_once()
    pipeline.multi.assert_not_called()
    pipeline.execute.assert_not_called()
    pipeline.reset.assert_called_once()
