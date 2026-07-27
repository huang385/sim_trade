from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, call

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.order import Order
from app.repositories.order_repository import OrderRepository
from app.services.active_order_rebuild_service import ActiveOrderRebuildService


def make_order(order_id, *, row_id=1, **overrides):
    values = {
        "id": row_id,
        "order_id": order_id,
        "client_order_id": f"CLIENT-{order_id}",
        "account_id": "A001",
        "order_book_id": "RB2610",
        "symbol": "RB2610",
        "exchange_id": "SHFE",
        "trading_day": date(2026, 7, 20),
        "direction": "BUY",
        "offset_flag": "OPEN",
        "order_type": "LIMIT",
        "limit_price": Decimal("3500"),
        "total_volume": 2,
        "traded_volume": 0,
        "remaining_volume": 2,
        "cancelled_volume": 0,
        "average_price": None,
        "status": "ACCEPTED",
        "submit_status": "ACCEPTED",
        "frozen_margin": Decimal("8400"),
        "frozen_commission": Decimal("6"),
        "frozen_position_volume": 0,
        "reject_code": None,
        "reject_message": None,
        "created_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "accepted_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return Order(**values)


def test_repository_cursor_query_only_returns_true_active_orders():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Order.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    orders = [
        make_order("ACCEPTED", row_id=1),
        make_order(
            "PARTIAL",
            row_id=2,
            status="PARTIALLY_FILLED",
            traded_volume=1,
            remaining_volume=1,
        ),
        make_order("FILLED", row_id=3, status="FILLED", traded_volume=2, remaining_volume=0),
        make_order("CANCELLED", row_id=4, status="CANCELLED", remaining_volume=0, cancelled_volume=2),
        make_order("PARTIAL_CANCELLED", row_id=5, status="PARTIALLY_CANCELLED", traded_volume=1, remaining_volume=0, cancelled_volume=1),
        make_order("REJECTED", row_id=6, status="REJECTED"),
        make_order("ZERO", row_id=7, traded_volume=2, remaining_volume=0),
        make_order("MARKET", row_id=8, order_type="MARKET"),
        make_order("CLOSE", row_id=9, offset_flag="CLOSE"),
    ]
    with factory() as db:
        db.add_all(orders)
        db.commit()
        result = OrderRepository.list_active_after_id(
            db,
            last_id=0,
            batch_size=100,
        )

    assert [order.order_id for order in result] == [
        "ACCEPTED",
        "PARTIAL",
        "CLOSE",
    ]


def test_rebuild_pages_and_upserts_database_active_orders():
    first = make_order("O-1", row_id=1)
    second = make_order(
        "O-2",
        row_id=2,
        status="PARTIALLY_FILLED",
        traded_volume=1,
        remaining_volume=1,
    )
    repository = Mock()
    repository.list_active_after_id.side_effect = [[first], [second], []]
    index = Mock()
    index.list_all_order_ids.return_value = set()
    service = ActiveOrderRebuildService(
        order_repository=repository,
        active_order_index=index,
        batch_size=1,
    )

    result = service.rebuild(Mock())

    assert result.scanned == 2
    assert result.upserted == 2
    assert index.upsert_active_order_for_rebuild.call_args_list == [
        call(first),
        call(second),
    ]
    assert repository.list_active_after_id.call_args_list[1].kwargs[
        "last_id"
    ] == 1


def test_rebuild_removes_terminal_and_missing_but_keeps_concurrent_active():
    terminal = make_order("TERMINAL", status="FILLED", traded_volume=2, remaining_volume=0)
    concurrent = make_order("CONCURRENT", row_id=99)
    repository = Mock()
    repository.list_active_after_id.return_value = []
    repository.get_by_order_id.side_effect = lambda _db, order_id: {
        "TERMINAL": terminal,
        "GHOST": None,
        "CONCURRENT": concurrent,
    }[order_id]
    index = Mock()
    index.list_all_order_ids.return_value = {
        "TERMINAL",
        "GHOST",
        "CONCURRENT",
    }
    index.get_active_order.side_effect = lambda order_id: {
        "TERMINAL": {
            "account_id": "A001",
            "exchange_id": "SHFE",
            "symbol": "RB2610",
        },
        "GHOST": {},
    }.get(order_id, {})
    service = ActiveOrderRebuildService(
        order_repository=repository,
        active_order_index=index,
    )

    result = service.rebuild(Mock())

    assert result.removed == 2
    assert result.skipped == 1
    index.remove_active_order.assert_called_once_with(
        order_id="TERMINAL",
        account_id="A001",
        exchange_id="SHFE",
        symbol="RB2610",
    )
    index.remove_orphan_order_id.assert_called_once_with("GHOST")
    index.upsert_active_order_for_rebuild.assert_called_once_with(concurrent)
