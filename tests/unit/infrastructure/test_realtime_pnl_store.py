from datetime import datetime, timezone
from decimal import Decimal
import json
from unittest.mock import Mock

import pytest
from redis.exceptions import WatchError

from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.schemas.pnl_schema import AccountRealtimePnl, PositionRealtimePnl


def test_trade_dirty_is_written_by_one_atomic_lua_command():
    redis_client = Mock()
    redis_client.eval.return_value = "12"
    store = RealtimePnlStore(redis_client)

    version = store.mark_contract_dirty(
        exchange_id="shfe",
        order_book_id="rb2610",
        account_id="A001",
    )

    assert version == "12"
    redis_client.eval.assert_called_once()
    args = redis_client.eval.call_args.args
    assert args[1] == 5
    assert "pnl:dirty_contracts" in args
    assert "pnl:dirty_contract_versions" in args
    assert "pnl:dirty_account_contracts:A001" in args


def test_contract_dirty_completion_cas_cleans_account_structure_only_on_match():
    redis_client = Mock()
    redis_client.eval.return_value = 1
    store = RealtimePnlStore(redis_client)

    assert store.complete_dirty_contract(
        exchange_id="SHFE",
        order_book_id="RB2610",
        expected_version="12",
    )

    args = redis_client.eval.call_args.args
    assert "SMEMBERS" in args[0]
    assert "pnl:dirty_account_contracts:" in args
    assert args[-2:] == ("12", "pnl:dirty_account_contracts:")


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
        order_book_id="RB2610",
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
        order_book_id="RB2610",
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
    assert redis_client.eval.call_args.args[-3] == "worker-old"


def test_token_guarded_cycle_write_uses_exact_owner_and_fencing_token():
    redis_client = Mock()
    redis_client.eval.return_value = 0
    store = RealtimePnlStore(redis_client)

    written, positions_written, accounts_written = (
        store.write_cycle_snapshots_if_lease_value_owned(
            lease_key="cash_valuation:writer:lease",
            lease_value="worker-b:102",
            positions=[],
            accounts=[],
            dirty_version="CASH:17",
            active_positions=[],
            closed_positions=[],
            mark_dirty=False,
        )
    )

    assert (written, positions_written, accounts_written) == (False, 0, 0)
    args = redis_client.eval.call_args.args
    assert args[2] == "cash_valuation:writer:lease"
    assert args[5] == "worker-b:102"
    assert "redis.call('GET', KEYS[1]) ~= ARGV[1]" in args[0]


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


def test_list_active_contract_codes_returns_order_book_ids_not_internal_symbols():
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
    assert pipeline.hget.call_count == 0
    # 读取路径只过滤空集合，不删除索引，避免并发新建持仓时误删新索引。
    redis_client.srem.assert_not_called()


def test_margin_dependency_codes_use_dedicated_index():
    redis_client = Mock()
    redis_client.smembers.return_value = {
        "000300.SH",
        "jd2609",
        "",
    }

    assert RealtimePnlStore(
        redis_client
    ).list_margin_dependency_codes() == {"000300.SH", "JD2609"}
    redis_client.pipeline.assert_not_called()


def test_rebuild_active_indexes_replaces_margin_dependencies_atomically():
    redis_client = Mock()
    pipeline = redis_client.pipeline.return_value
    pipeline.get.return_value = "3"
    pipeline.smembers.return_value = set()
    pipeline.execute.return_value = []

    rebuilt = RealtimePnlStore(redis_client).rebuild_active_indexes(
        expected_cache_version="3",
        positions=[("A001", "DCE", "JD2609P3500", "P001")],
        margin_dependency_codes=["jd2609", "JD2609", "", "000300.SH"],
    )

    assert rebuilt is True
    deleted = pipeline.delete.call_args.args
    assert "pnl:margin_dependency_codes" in deleted
    pipeline.sadd.assert_any_call(
        "pnl:margin_dependency_codes",
        "000300.SH",
        "JD2609",
    )


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


def test_cycle_snapshot_hash_version_and_event_share_one_lua_script():
    redis_client = Mock()
    redis_client.eval.return_value = "41"
    store = RealtimePnlStore(redis_client)
    now = datetime.now(timezone.utc)
    position = PositionRealtimePnl(
        position_id="P001",
        account_id="A001",
        exchange_id="DCE",
        symbol="JD2609",
        direction="LONG",
        mark_price=Decimal("3500"),
        cumulative_unrealized_pnl=Decimal("10"),
        daily_position_pnl=Decimal("10"),
        event_time=now,
        source_event_id="TICK-1",
        updated_at=now,
        source_position_fact_version="80",
    )
    account = AccountRealtimePnl(
        account_id="A001",
        cumulative_unrealized_pnl=Decimal("10"),
        daily_position_pnl=Decimal("10"),
        daily_close_pnl=Decimal("0"),
        daily_commission=Decimal("1"),
        daily_pnl=Decimal("9"),
        equity=Decimal("100010"),
        available_cash=Decimal("90000"),
        risk_ratio=Decimal("0.1"),
        risk_available_cash=Decimal("89000"),
        risk_state="MARGIN_DEFICIT",
        updated_at=now,
        source_account_fact_version="70",
    )

    store.write_cycle_snapshots(
        positions=[position],
        accounts=[account],
        dirty_version="market-1",
        active_positions=[],
        closed_positions=[],
    )

    args = redis_client.eval.call_args.args
    assert args[1] == 2
    assert args[2] == "pnl:realtime:snapshot_sequence"
    script = args[0]
    assert "HSET_REALTIME_SNAPSHOT" in script
    assert "realtime_snapshot_version" in script
    assert "XADD" in script
    assert "realtime_version" in script
    operations = json.loads(args[-1])
    snapshot_operations = [
        operation
        for operation in operations
        if operation[0] == "HSET_REALTIME_SNAPSHOT"
    ]
    assert snapshot_operations[0][2] == "pnl:realtime:position_versions"
    assert snapshot_operations[1][2] == "pnl:realtime:account_versions"
    account_events = [
        operation
        for operation in operations
        if operation[0] == "XADD_REALTIME_EVENT"
        and operation[3] == "ACCOUNT_PNL_UPDATED"
    ]
    assert len(account_events) == 1
    account_payload = json.loads(account_events[0][6])["payload"]
    assert account_payload["source_account_fact_version"] == "70"
    # 桌面端资金页依赖实时手续费更新，daily_commission随账户PnL事件下发。
    assert "daily_commission" in account_payload
    assert {
        "risk_state",
        "risk_ratio",
        "risk_available_cash",
        "cash_balance",
        "used_margin",
        "daily_close_pnl",
    }.isdisjoint(account_payload)
    risk_events = [
        operation
        for operation in operations
        if operation[0] == "XADD_REALTIME_EVENT"
        and operation[3] == "RISK_STATE_CHANGED"
    ]
    assert len(risk_events) == 1
    risk_payload = json.loads(risk_events[0][6])["payload"]
    assert risk_payload == {
        "risk_state": "MARGIN_DEFICIT",
        "risk_ratio": "0.1",
        "risk_available_cash": "89000",
        "updated_at": now.isoformat(),
    }


def test_cycle_write_rejects_changed_position_cache_version():
    redis_client = Mock()
    redis_client.eval.return_value = 0
    store = RealtimePnlStore(redis_client)

    with pytest.raises(RuntimeError, match="持仓事实版本已变化"):
        store.write_cycle_snapshots(
            positions=[],
            accounts=[],
            dirty_version="old-cycle",
            active_positions=[],
            closed_positions=[],
            expected_cache_version="8",
        )

    args = redis_client.eval.call_args.args
    assert args[-2] == "8"


def test_versioned_snapshot_batch_read_uses_one_pipeline():
    redis_client = Mock()
    pipeline = redis_client.pipeline.return_value
    pipeline.execute.return_value = [
        {"account_id": "A001", "realtime_snapshot_version": "7"},
        {"position_id": "P001", "realtime_snapshot_version": "7"},
        {"position_id": "P002", "realtime_snapshot_version": "6"},
        ["7"],
        ["7", "6"],
        False,
        0,
    ]
    store = RealtimePnlStore(redis_client)

    (
        accounts,
        positions,
        account_versions,
        position_versions,
        dirty_facts,
        dirty_structures,
    ) = (
        store.get_accounts_with_positions_and_versions(
            account_ids=["A001"],
            position_ids=["P001", "P002"],
        )
    )

    assert accounts["A001"]["realtime_snapshot_version"] == "7"
    assert positions["P002"]["realtime_snapshot_version"] == "6"
    assert account_versions == {"A001": "7"}
    assert position_versions == {"P001": "7", "P002": "6"}
    assert dirty_facts == set()
    assert dirty_structures == set()
    redis_client.pipeline.assert_called_once_with(transaction=False)
    assert pipeline.hgetall.call_count == 3
    assert pipeline.hmget.call_count == 2
    pipeline.sismember.assert_called_once()
    pipeline.scard.assert_called_once()
    pipeline.execute.assert_called_once()


def test_versioned_snapshot_batch_read_returns_related_dirty_accounts():
    redis_client = Mock()
    pipeline = redis_client.pipeline.return_value
    pipeline.execute.return_value = [
        {"account_id": "A001", "realtime_snapshot_version": "8"},
        {"account_id": "A002", "realtime_snapshot_version": "8"},
        ["8", "8"],
        [],
        True,
        False,
        0,
        2,
    ]

    result = RealtimePnlStore(
        redis_client
    ).get_accounts_with_positions_and_versions(
        account_ids=["A001", "A002"],
        position_ids=[],
    )

    assert result[4] == {"A001"}
    assert result[5] == {"A002"}
    pipeline.execute.assert_called_once()


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


def test_dirty_position_without_version_is_atomically_pruned():
    redis_client = Mock()
    redis_client.eval.side_effect = [[], 1]
    redis_client.get.return_value = "0"
    redis_client.sscan.return_value = (0, ["P-ORPHAN"])
    redis_client.hmget.return_value = [None]

    result = RealtimePnlStore(redis_client).list_dirty_positions(10)

    assert result == []
    assert redis_client.eval.call_count == 2
    assert redis_client.eval.call_args.args[-1] == "P-ORPHAN"


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


def test_dirty_account_without_version_is_atomically_pruned():
    redis_client = Mock()
    redis_client.eval.side_effect = [[], 1]
    redis_client.get.return_value = "0"
    redis_client.sscan.return_value = (0, ["A-ORPHAN"])
    redis_client.hmget.return_value = [None]

    result = RealtimePnlStore(redis_client).list_dirty_accounts(10)

    assert result == []
    assert redis_client.eval.call_count == 2
    assert redis_client.eval.call_args.args[-1] == "A-ORPHAN"


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


def test_daily_settlement_rebuild_clears_accounts_without_positions():
    redis_client = Mock()
    pipeline = redis_client.pipeline.return_value
    pipeline.execute.return_value = []
    store = RealtimePnlStore(redis_client)
    store.bump_position_cache_version = Mock(return_value="9")
    store.rebuild_active_indexes = Mock(return_value=True)
    store.mark_contract_dirty = Mock()

    store.rebuild_after_daily_settlement(
        active_positions=[],
        affected_positions=[],
        affected_account_ids=["A-EMPTY"],
    )

    pipeline.delete.assert_called_once_with("pnl:account:A-EMPTY")
    store.rebuild_active_indexes.assert_called_once_with(
        expected_cache_version="9",
        positions=[],
    )
