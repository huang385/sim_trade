from types import SimpleNamespace
from unittest.mock import Mock

from app.repositories.order_repository import OrderRepository
from app.repositories.trade_repository import TradeRepository


def _database_rows(rows):
    db = Mock()
    db.scalars.return_value.all.return_value = rows
    return db


def test_order_query_defaults_to_bounded_latest_page():
    newest = SimpleNamespace(id=3)
    older = SimpleNamespace(id=2)
    db = _database_rows([newest, older])

    result = OrderRepository.list_by_account(
        db,
        "A001",
        limit=2,
    )

    # 数据库倒序读取最近记录，响应再恢复为时间正序。
    assert result == [older, newest]
    statement = db.scalars.call_args.args[0]
    sql = str(statement)
    assert "ORDER BY orders.id DESC" in sql
    assert "LIMIT" in sql


def test_order_query_uses_after_id_without_offset():
    db = _database_rows([SimpleNamespace(id=11)])

    OrderRepository.list_by_account(
        db,
        "A001",
        after_id=10,
        limit=100,
    )

    sql = str(db.scalars.call_args.args[0])
    assert "orders.id >" in sql
    assert "ORDER BY orders.id" in sql
    assert "OFFSET" not in sql


def test_trade_query_defaults_to_bounded_latest_page():
    newest = SimpleNamespace(id=5)
    older = SimpleNamespace(id=4)
    db = _database_rows([newest, older])

    result = TradeRepository.list(
        db,
        account_id="A001",
        limit=2,
    )

    assert result == [older, newest]
    sql = str(db.scalars.call_args.args[0])
    assert "ORDER BY trade.id DESC" in sql
    assert "LIMIT" in sql


def test_trade_query_uses_after_id_without_offset():
    db = _database_rows([SimpleNamespace(id=21)])

    TradeRepository.list(
        db,
        account_id="A001",
        after_id=20,
        limit=100,
    )

    sql = str(db.scalars.call_args.args[0])
    assert "trade.id >" in sql
    assert "ORDER BY trade.id" in sql
    assert "OFFSET" not in sql
