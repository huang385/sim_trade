from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, call

from app.services.pnl_snapshot_persistence_service import (
    PnlSnapshotPersistenceService,
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
    )
    return position, detail


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
    position_repository.list_by_position_ids_for_update.return_value = [
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
            contract_multiplier=Decimal("1"),
        ),
        SimpleNamespace(
            order_book_id="JM2609",
            contract_multiplier=Decimal("1"),
        ),
    ]
    market_tick_store = Mock()
    market_tick_store.get_latest_many.return_value = {
        ("DCE", "JD2609"): {
            "source": "YML_FEEDHUB",
            "ingest_type": "LIVE_CALLBACK",
            "last_price": "110",
        },
        ("DCE", "JM2609"): {
            "source": "YML_FEEDHUB",
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
    position_repository.list_by_position_ids_for_update.assert_called_once()
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
    pnl_store.complete_dirty_position.return_value = True
    position_repository = Mock()
    position_repository.list_account_ids_for_positions.return_value = [
        ("P-A", "A001"),
        ("P-B", "B001"),
    ]
    position_repository.list_by_position_ids_for_update.side_effect = [
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
                contract_multiplier=Decimal("1"),
            )
        ],
        [
            SimpleNamespace(
                order_book_id="JM2609",
                contract_multiplier=Decimal("1"),
            )
        ],
    ]
    market_tick_store = Mock()
    market_tick_store.get_latest_many.side_effect = [
        {
            ("DCE", "JD2609"): {
                "source": "YML_FEEDHUB",
                "ingest_type": "LIVE_CALLBACK",
                "last_price": "110",
            }
        },
        {
            ("DCE", "JM2609"): {
                "source": "YML_FEEDHUB",
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
    pnl_store.complete_dirty_account.assert_called_once_with("A001")
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
    position_repository.list_by_position_ids_for_update.return_value = [
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
            contract_multiplier=Decimal("1"),
        )
    ]
    market_tick_store = Mock()
    market_tick_store.get_latest_many.return_value = {
        ("DCE", "JD2609"): {
            "source": "YML_FEEDHUB",
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
