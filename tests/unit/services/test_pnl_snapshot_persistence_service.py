from contextlib import nullcontext
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, call

from app.services.pnl_snapshot_persistence_service import (
    PnlSnapshotPersistenceService,
)


def test_old_trading_day_market_price_is_rejected():
    assert (
        PnlSnapshotPersistenceService._mark_price(
            {
                "source": "YMM_LIVE_DATA",
                "ingest_type": "LIVE_CALLBACK",
                "trading_day": "2026-08-06",
                "last_price": "100",
            },
            expected_trading_day=date(2026, 8, 7),
        )
        is None
    )


def make_position(
    position_id: str,
    symbol: str,
    open_price: str,
    *,
    account_id: str = "A001",
):
    position = SimpleNamespace(
        position_id=position_id,
        account_id=account_id,
        order_book_id=symbol,
        exchange_id="DCE",
        symbol=symbol,
        direction="LONG",
        instrument_type="FUTURES",
        multiplier_snapshot=Decimal("1"),
        realtime_required_margin=Decimal("0"),
        option_market_value=Decimal("0"),
        total_volume=1,
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        updated_at=None,
    )
    detail = SimpleNamespace(
        position_detail_id=f"D-{position_id}",
        position_id=position_id,
        open_price=Decimal(open_price),
        pnl_base_price=Decimal(open_price),
        remaining_volume=1,
        multiplier_snapshot=Decimal("1"),
    )
    return position, detail


def make_account(account_id: str = "A001"):
    return SimpleNamespace(
        account_id=account_id,
        cash_balance=Decimal("100000"),
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        daily_close_pnl=Decimal("0"),
        daily_commission=Decimal("0"),
        daily_pnl=Decimal("0"),
        equity=Decimal("100000"),
        available_cash=Decimal("90000"),
        risk_available_cash=Decimal("90000"),
        used_margin=Decimal("10000"),
        option_used_margin=Decimal("0"),
        option_realtime_required_margin=Decimal("0"),
        frozen_margin=Decimal("0"),
        frozen_cash=Decimal("0"),
        frozen_commission=Decimal("0"),
        long_option_market_value=Decimal("0"),
        short_option_market_value=Decimal("0"),
        net_option_market_value=Decimal("0"),
        risk_state="NORMAL",
        risk_ratio=Decimal("0"),
        updated_at=None,
    )


def build_account_only_persistence(*, order_risk_state: str):
    position_repository = Mock()
    position_repository.list_active_by_account_for_update.return_value = []
    position_repository.list_open_details_by_position_ids_for_update.return_value = []
    instrument_repository = Mock()
    instrument_repository.list_by_order_book_ids.return_value = []
    instrument_repository.list_by_ids.return_value = []
    order_repository = Mock()
    order_repository.list_active_option_sell_open_by_account.return_value = [
        SimpleNamespace(margin_risk_state=order_risk_state)
    ]
    return PnlSnapshotPersistenceService(
        session_factory=Mock(),
        pnl_store=Mock(),
        market_tick_store=Mock(),
        position_repository=position_repository,
        instrument_repository=instrument_repository,
        order_repository=order_repository,
    )


def test_active_order_margin_deficit_survives_complete_position_valuation():
    account = make_account()
    service = build_account_only_persistence(
        order_risk_state="MARGIN_DEFICIT"
    )

    complete = service._recalculate_locked_account(Mock(), account)

    assert complete is True
    assert account.risk_state == "MARGIN_DEFICIT"


def test_active_order_unavailable_keeps_account_dirty_and_risk_state():
    account = make_account()
    service = build_account_only_persistence(
        order_risk_state="VALUATION_UNAVAILABLE"
    )

    complete = service._recalculate_locked_account(Mock(), account)

    assert complete is False
    assert account.risk_state == "VALUATION_UNAVAILABLE"


def test_same_account_positions_are_loaded_and_persisted_in_batches():
    first, first_detail = make_position("P1", "JD2609", "100")
    second, second_detail = make_position("P2", "JM2609", "200")
    account = SimpleNamespace(
        account_id="A001",
        cash_balance=Decimal("100000"),
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        daily_close_pnl=Decimal("0"),
        daily_commission=Decimal("0"),
        daily_pnl=Decimal("0"),
        equity=Decimal("100000"),
        available_cash=Decimal("90000"),
        used_margin=Decimal("10000"),
        frozen_margin=Decimal("0"),
        frozen_cash=Decimal("0"),
        frozen_commission=Decimal("0"),
        risk_ratio=Decimal("0"),
        option_used_margin=Decimal("0"),
        option_realtime_required_margin=Decimal("0"),
        long_option_market_value=Decimal("0"),
        short_option_market_value=Decimal("0"),
        net_option_market_value=Decimal("0"),
        risk_available_cash=Decimal("90000"),
        risk_state="NORMAL",
        updated_at=None,
    )

    mapping_db = Mock()
    account_db = Mock()
    session_factory = Mock(
        side_effect=[
            nullcontext(mapping_db),
            nullcontext(account_db),
        ]
    )
    pnl_store = Mock()
    pnl_store.list_dirty_positions.return_value = [
        ("P1", "v1"),
        ("P2", "v2"),
    ]
    pnl_store.complete_dirty_position.return_value = True
    position_repository = Mock()
    position_repository.list_account_ids_for_positions.return_value = [
        ("P1", "A001"),
        ("P2", "A001"),
    ]
    position_repository.list_active_by_account_for_update.return_value = [
        first,
        second,
    ]
    position_repository.list_open_details_by_position_ids_for_update.return_value = [
        first_detail,
        second_detail,
    ]
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = account
    instrument_repository = Mock()
    instrument_repository.list_by_order_book_ids.return_value = [
        SimpleNamespace(
            order_book_id="JD2609",
            exchange_id="DCE",
            symbol="JD2609",
            underlying_instrument_id=None,
            contract_multiplier=Decimal("1"),
        ),
        SimpleNamespace(
            order_book_id="JM2609",
            exchange_id="DCE",
            symbol="JM2609",
            underlying_instrument_id=None,
            contract_multiplier=Decimal("1"),
        ),
    ]
    instrument_repository.list_by_ids.return_value = []
    market_tick_store = Mock()
    market_tick_store.get_latest_many.return_value = {
        ("DCE", "JD2609"): {
            "source": "YMM_LIVE_DATA",
            "ingest_type": "LIVE_CALLBACK",
            "last_price": "110",
        },
        ("DCE", "JM2609"): {
            "source": "YMM_LIVE_DATA",
            "ingest_type": "LIVE_CALLBACK",
            "last_price": "220",
        },
    }

    result = PnlSnapshotPersistenceService(
        session_factory=session_factory,
        pnl_store=pnl_store,
        market_tick_store=market_tick_store,
        account_repository=account_repository,
        position_repository=position_repository,
        instrument_repository=instrument_repository,
    ).persist_batch(500)

    assert result.positions_persisted == 2
    account_repository.get_by_account_id_for_update.assert_called_once()
    position_repository.list_active_by_account_for_update.assert_called_once()
    position_repository.list_open_details_by_position_ids_for_update.assert_called_once()
    instrument_repository.list_by_order_book_ids.assert_called_once()
    market_tick_store.get_latest_many.assert_called_once()
    position_repository.get_by_position_id_for_update.assert_not_called()
    position_repository.list_open_details_for_update.assert_not_called()
    account_db.commit.assert_called_once()
    assert pnl_store.complete_dirty_position.call_args_list == [
        call("P1", "v1"),
        call("P2", "v2"),
    ]


def test_only_successfully_committed_account_positions_clear_dirty():
    first, first_detail = make_position(
        "P-A",
        "JD2609",
        "100",
        account_id="A001",
    )
    second, second_detail = make_position(
        "P-B",
        "JM2609",
        "200",
        account_id="B001",
    )

    def account(account_id):
        return SimpleNamespace(
            account_id=account_id,
            cash_balance=Decimal("100000"),
            unrealized_pnl=Decimal("0"),
            daily_position_pnl=Decimal("0"),
            daily_close_pnl=Decimal("0"),
            daily_commission=Decimal("0"),
            daily_pnl=Decimal("0"),
            equity=Decimal("100000"),
            available_cash=Decimal("90000"),
            used_margin=Decimal("10000"),
            frozen_margin=Decimal("0"),
            frozen_cash=Decimal("0"),
            frozen_commission=Decimal("0"),
                risk_ratio=Decimal("0"),
                option_used_margin=Decimal("0"),
                option_realtime_required_margin=Decimal("0"),
                long_option_market_value=Decimal("0"),
                short_option_market_value=Decimal("0"),
                net_option_market_value=Decimal("0"),
                risk_available_cash=Decimal("90000"),
                risk_state="NORMAL",
            updated_at=None,
        )

    mapping_db = Mock()
    account_a_db = Mock()
    account_b_db = Mock()
    account_b_db.commit.side_effect = RuntimeError("commit failed")
    session_factory = Mock(
        side_effect=[
            nullcontext(mapping_db),
            nullcontext(account_a_db),
            nullcontext(account_b_db),
        ]
    )
    pnl_store = Mock()
    pnl_store.list_dirty_positions.return_value = [
        ("P-A", "version-a"),
        ("P-B", "version-b"),
    ]
    pnl_store.list_dirty_accounts.return_value = [
        ("A001", "account-version-a"),
        ("B001", "account-version-b"),
    ]
    pnl_store.complete_dirty_position.return_value = True
    position_repository = Mock()
    position_repository.list_account_ids_for_positions.return_value = [
        ("P-A", "A001"),
        ("P-B", "B001"),
    ]
    position_repository.list_active_by_account_for_update.side_effect = [
        [first],
        [second],
    ]
    position_repository.list_open_details_by_position_ids_for_update.side_effect = [
        [first_detail],
        [second_detail],
    ]
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.side_effect = [
        account("A001"),
        account("B001"),
    ]
    instrument_repository = Mock()
    instrument_repository.list_by_order_book_ids.side_effect = [
        [
            SimpleNamespace(
                order_book_id="JD2609",
                exchange_id="DCE",
                symbol="JD2609",
                underlying_instrument_id=None,
                contract_multiplier=Decimal("1"),
            )
        ],
        [
            SimpleNamespace(
                order_book_id="JM2609",
                exchange_id="DCE",
                symbol="JM2609",
                underlying_instrument_id=None,
                contract_multiplier=Decimal("1"),
            )
        ],
    ]
    instrument_repository.list_by_ids.return_value = []
    market_tick_store = Mock()
    market_tick_store.get_latest_many.side_effect = [
        {
            ("DCE", "JD2609"): {
                "source": "YMM_LIVE_DATA",
                "ingest_type": "LIVE_CALLBACK",
                "last_price": "110",
            }
        },
        {
            ("DCE", "JM2609"): {
                "source": "YMM_LIVE_DATA",
                "ingest_type": "LIVE_CALLBACK",
                "last_price": "220",
            }
        },
    ]

    result = PnlSnapshotPersistenceService(
        session_factory=session_factory,
        pnl_store=pnl_store,
        market_tick_store=market_tick_store,
        account_repository=account_repository,
        position_repository=position_repository,
        instrument_repository=instrument_repository,
    ).persist_batch(500)

    assert result.positions_persisted == 1
    assert result.accounts_persisted == 1
    pnl_store.complete_dirty_position.assert_called_once_with(
        "P-A",
        "version-a",
    )
    pnl_store.complete_dirty_account.assert_called_once_with(
        "A001",
        "account-version-a",
    )
    account_a_db.commit.assert_called_once()
    account_b_db.commit.assert_called_once()


def test_dirty_version_change_keeps_new_version_after_database_commit():
    first, first_detail = make_position("P1", "JD2609", "100")
    account = SimpleNamespace(
        account_id="A001",
        cash_balance=Decimal("100000"),
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        daily_close_pnl=Decimal("0"),
        daily_commission=Decimal("0"),
        daily_pnl=Decimal("0"),
        equity=Decimal("100000"),
        available_cash=Decimal("90000"),
        used_margin=Decimal("10000"),
        frozen_margin=Decimal("0"),
        frozen_cash=Decimal("0"),
        frozen_commission=Decimal("0"),
        risk_ratio=Decimal("0"),
        option_used_margin=Decimal("0"),
        option_realtime_required_margin=Decimal("0"),
        long_option_market_value=Decimal("0"),
        short_option_market_value=Decimal("0"),
        net_option_market_value=Decimal("0"),
        risk_available_cash=Decimal("90000"),
        risk_state="NORMAL",
        updated_at=None,
    )
    mapping_db = Mock()
    account_db = Mock()
    pnl_store = Mock()
    pnl_store.list_dirty_positions.return_value = [("P1", "old-version")]
    # CAS返回False表示持久化期间Tick写入了新版本，不能清掉新Dirty。
    pnl_store.complete_dirty_position.return_value = False
    position_repository = Mock()
    position_repository.list_account_ids_for_positions.return_value = [
        ("P1", "A001")
    ]
    position_repository.list_active_by_account_for_update.return_value = [
        first
    ]
    position_repository.list_open_details_by_position_ids_for_update.return_value = [
        first_detail
    ]
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = account
    instrument_repository = Mock()
    instrument_repository.list_by_order_book_ids.return_value = [
        SimpleNamespace(
            order_book_id="JD2609",
            exchange_id="DCE",
            symbol="JD2609",
            underlying_instrument_id=None,
            contract_multiplier=Decimal("1"),
        )
    ]
    instrument_repository.list_by_ids.return_value = []
    market_tick_store = Mock()
    market_tick_store.get_latest_many.return_value = {
        ("DCE", "JD2609"): {
            "source": "YMM_LIVE_DATA",
            "ingest_type": "LIVE_CALLBACK",
            "last_price": "110",
        }
    }

    result = PnlSnapshotPersistenceService(
        session_factory=Mock(
            side_effect=[
                nullcontext(mapping_db),
                nullcontext(account_db),
            ]
        ),
        pnl_store=pnl_store,
        market_tick_store=market_tick_store,
        account_repository=account_repository,
        position_repository=position_repository,
        instrument_repository=instrument_repository,
    ).persist_batch(500)

    account_db.commit.assert_called_once()
    pnl_store.complete_dirty_position.assert_called_once_with(
        "P1",
        "old-version",
    )
    assert result.positions_persisted == 0
    assert result.retained == 1


def test_account_dirty_without_position_dirty_is_persisted_and_cas_cleared():
    mapping_db = Mock()
    account_db = Mock()
    pnl_store = Mock()
    pnl_store.list_dirty_positions.return_value = []
    pnl_store.list_dirty_accounts.return_value = [("A001", "account-v1")]
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = make_account()
    position_repository = Mock()
    position_repository.list_account_ids_for_positions.return_value = []
    position_repository.list_active_by_account_for_update.return_value = []
    position_repository.list_open_details_by_position_ids_for_update.return_value = []
    instrument_repository = Mock()
    instrument_repository.list_by_order_book_ids.return_value = []
    instrument_repository.list_by_ids.return_value = []

    result = PnlSnapshotPersistenceService(
        session_factory=Mock(
            side_effect=[nullcontext(mapping_db), nullcontext(account_db)]
        ),
        pnl_store=pnl_store,
        market_tick_store=Mock(),
        account_repository=account_repository,
        position_repository=position_repository,
        instrument_repository=instrument_repository,
    ).persist_batch(500)

    assert result.accounts_requested == 1
    assert result.accounts_persisted == 1
    account_db.commit.assert_called_once()
    pnl_store.complete_dirty_account.assert_called_once_with(
        "A001", "account-v1"
    )


def test_deleted_position_dirty_is_cas_cleared_without_retrying_forever():
    """PostgreSQL 已不存在的持仓 Dirty 应按原版本安全清理。"""

    pnl_store = Mock()
    pnl_store.list_dirty_positions.return_value = [("P-DELETED", "v1")]
    pnl_store.list_dirty_accounts.return_value = []
    pnl_store.complete_dirty_position.return_value = True
    position_repository = Mock()
    position_repository.list_account_ids_for_positions.return_value = []

    result = PnlSnapshotPersistenceService(
        session_factory=Mock(return_value=nullcontext(Mock())),
        pnl_store=pnl_store,
        market_tick_store=Mock(),
        position_repository=position_repository,
    ).persist_batch(500)

    pnl_store.complete_dirty_position.assert_called_once_with(
        "P-DELETED", "v1"
    )
    assert result.positions_persisted == 1
    assert result.retained == 0


def test_deleted_account_dirty_is_cas_cleared_without_business_write():
    """账户已删除时结束本版本 Dirty，不执行任何估值写入。"""

    mapping_db = Mock()
    account_db = Mock()
    pnl_store = Mock()
    pnl_store.list_dirty_positions.return_value = []
    pnl_store.list_dirty_accounts.return_value = [("A-DELETED", "v7")]
    position_repository = Mock()
    position_repository.list_account_ids_for_positions.return_value = []
    account_repository = Mock()
    account_repository.get_by_account_id_for_update.return_value = None

    result = PnlSnapshotPersistenceService(
        session_factory=Mock(
            side_effect=[nullcontext(mapping_db), nullcontext(account_db)]
        ),
        pnl_store=pnl_store,
        market_tick_store=Mock(),
        account_repository=account_repository,
        position_repository=position_repository,
    ).persist_batch(500)

    account_db.rollback.assert_called_once()
    account_db.commit.assert_not_called()
    pnl_store.complete_dirty_account.assert_called_once_with(
        "A-DELETED", "v7"
    )
    assert result.accounts_persisted == 0


def test_missing_market_persists_unavailable_state_and_keeps_dirty_version():
    position, detail = make_position("P1", "JD2609", "100")
    account = make_account()
    pnl_store = Mock()
    pnl_store.list_dirty_positions.return_value = []
    pnl_store.list_dirty_accounts.return_value = [("A001", "account-v1")]
    position_repository = Mock()
    position_repository.list_account_ids_for_positions.return_value = []
    position_repository.list_active_by_account_for_update.return_value = [
        position
    ]
    position_repository.list_open_details_by_position_ids_for_update.return_value = [
        detail
    ]
    instrument_repository = Mock()
    instrument_repository.list_by_order_book_ids.return_value = [
        SimpleNamespace(
            id=1,
            order_book_id="JD2609",
            exchange_id="DCE",
            symbol="JD2609",
            underlying_instrument_id=None,
        )
    ]
    instrument_repository.list_by_ids.return_value = []
    account_db = Mock()

    result = PnlSnapshotPersistenceService(
        session_factory=Mock(
            side_effect=[nullcontext(Mock()), nullcontext(account_db)]
        ),
        pnl_store=pnl_store,
        market_tick_store=Mock(
            get_latest_many=Mock(return_value={})
        ),
        account_repository=Mock(
            get_by_account_id_for_update=Mock(return_value=account)
        ),
        position_repository=position_repository,
        instrument_repository=instrument_repository,
    ).persist_batch(500)

    assert result.accounts_persisted == 1
    assert account.risk_state == "VALUATION_UNAVAILABLE"
    account_db.commit.assert_called_once()
    pnl_store.complete_dirty_account.assert_not_called()
    pnl_store.get_accounts_many.assert_not_called()
    pnl_store.get_positions_many.assert_not_called()


def test_option_amounts_are_recalculated_from_database_facts_not_redis():
    """即使Redis金额被篡改，落库仍按锁定后的持仓、规则和行情重算。"""

    account = make_account()
    account.cash_balance = Decimal("103000")
    account.used_margin = Decimal("10000")
    account.option_used_margin = Decimal("10000")
    position = SimpleNamespace(
        position_id="P-OPT",
        account_id="A001",
        order_book_id="JD2609-C-4000",
        exchange_id="DCE",
        symbol="JD2609-C-4000",
        direction="SHORT",
        instrument_type="FUTURES_OPTION",
        multiplier_snapshot=Decimal("15"),
        total_volume=2,
        used_margin=Decimal("10000"),
        realtime_required_margin=Decimal("10000"),
        option_market_value=Decimal("3000"),
        margin_rule_id=7,
        margin_rule_version="V1",
        margin_rule_snapshot={
            "rule_id": 7,
            "rule_version": "V1",
            "margin_algorithm": "COMMODITY_FUTURES_OPTION",
            "margin_adjustment_rate": "1",
            "minimum_guarantee_rate": "0",
            "out_of_money_deduction_rate": "1",
            "minimum_underlying_margin_ratio": "0.5",
            "extra_margin_rate": "0",
            "underlying_margin_rate": "0.1",
            "underlying_multiplier": "10",
        },
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        updated_at=None,
    )
    detail = SimpleNamespace(
        position_detail_id="PD-OPT",
        position_id="P-OPT",
        open_price=Decimal("100"),
        pnl_base_price=Decimal("100"),
        remaining_volume=2,
        realtime_required_margin=Decimal("10000"),
        multiplier_snapshot=Decimal("15"),
        margin_rule_id=7,
        margin_rule_version="V1",
        margin_rule_snapshot=position.margin_rule_snapshot,
        margin_price_mode=None,
        margin_option_price=None,
        margin_underlying_price=None,
        margin_calculated_at=None,
        updated_at=None,
    )
    option = SimpleNamespace(
        id=1,
        order_book_id="JD2609-C-4000",
        exchange_id="DCE",
        symbol="JD2609-C-4000",
        instrument_type="FUTURES_OPTION",
        underlying_instrument_id=2,
        option_type="CALL",
        strike_price=Decimal("4000"),
    )
    underlying = SimpleNamespace(
        id=2,
        order_book_id="JD2609",
        exchange_id="DCE",
        symbol="JD2609",
        instrument_type="FUTURES",
        underlying_instrument_id=None,
    )
    pnl_store = Mock()
    pnl_store.list_dirty_positions.return_value = []
    pnl_store.list_dirty_accounts.return_value = [("A001", "v1")]
    # 这些伪造金额故意与数据库事实不同；生产服务不应读取它们。
    pnl_store.get_accounts_many.return_value = {
        "A001": {"short_option_market_value": "999999999"}
    }
    pnl_store.get_positions_many.return_value = {
        "P-OPT": {"realtime_required_margin": "1"}
    }
    position_repository = Mock()
    position_repository.list_account_ids_for_positions.return_value = []
    position_repository.list_active_by_account_for_update.return_value = [
        position
    ]
    position_repository.list_open_details_by_position_ids_for_update.return_value = [
        detail
    ]
    instrument_repository = Mock()
    instrument_repository.list_by_order_book_ids.return_value = [option]
    instrument_repository.list_by_ids.return_value = [underlying]

    PnlSnapshotPersistenceService(
        session_factory=Mock(
            side_effect=[nullcontext(Mock()), nullcontext(Mock())]
        ),
        pnl_store=pnl_store,
        market_tick_store=Mock(
            get_latest_many=Mock(
                return_value={
                    ("DCE", "JD2609-C-4000"): {
                        "source": "YMM_LIVE_DATA",
                        "ingest_type": "LIVE_CALLBACK",
                        "last_price": "105",
                    },
                    ("DCE", "JD2609"): {
                        "source": "YMM_LIVE_DATA",
                        "ingest_type": "LIVE_CALLBACK",
                        "last_price": "4033",
                    },
                }
            )
        ),
        account_repository=Mock(
            get_by_account_id_for_update=Mock(return_value=account)
        ),
        position_repository=position_repository,
        instrument_repository=instrument_repository,
    ).persist_batch(500)

    assert position.option_market_value == Decimal("3150.000000")
    assert position.realtime_required_margin == Decimal("11216.000000")
    assert detail.realtime_required_margin == Decimal("11216.000000")
    assert account.short_option_market_value == Decimal("3150.000000")
    assert account.option_realtime_required_margin == Decimal(
        "11216.000000"
    )
    pnl_store.get_accounts_many.assert_not_called()
    pnl_store.get_positions_many.assert_not_called()
