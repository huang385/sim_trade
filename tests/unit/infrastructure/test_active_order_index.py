from types import SimpleNamespace
from unittest.mock import Mock

from app.infrastructure.active_order_index import ActiveOrderIndex


def make_order(order_id: str = "O-1"):
    return SimpleNamespace(
        order_id=order_id,
        account_id="A001",
        exchange_id="DCE",
        symbol="JD2609",
        order_book_id="JD2609",
    )


def test_add_updates_active_contract_set_in_same_lua_command():
    redis_client = Mock()
    redis_client.eval.return_value = 1
    index = ActiveOrderIndex(redis_client)

    assert index.add_active_order(
        make_order(),
        event_id="E-1",
        processed_ttl_seconds=60,
    )

    arguments = redis_client.eval.call_args.args
    assert arguments[1] == 7
    assert "active_order_contracts" in arguments
    assert "DCE|JD2609|JD2609" in arguments


def test_remove_checks_contract_set_and_contract_member_in_same_lua():
    redis_client = Mock()
    redis_client.hgetall.return_value = {"order_book_id": "JD2609"}
    index = ActiveOrderIndex(redis_client)

    index.remove_active_order(
        order_id="O-1",
        account_id="A001",
        exchange_id="DCE",
        symbol="JD2609",
    )

    arguments = redis_client.eval.call_args.args
    assert arguments[1] == 7
    assert "SCARD" in arguments[0]
    assert "active_order_contracts" in arguments


def test_option_sell_open_adds_underlying_dependency_in_same_lua():
    redis_client = Mock()
    redis_client.eval.return_value = 1
    order = make_order()
    order.instrument_type = "FUTURES_OPTION"
    order.direction = "SELL"
    order.offset_flag = "OPEN"
    order.underlying_exchange_id = "DCE"
    order.underlying_symbol = "JD2609"

    ActiveOrderIndex(redis_client).add_active_order(
        order,
        event_id="E-OPTION",
        processed_ttl_seconds=60,
    )

    assert (
        "valuation:underlying_sell_open_orders:DCE:JD2609"
        in redis_client.eval.call_args.args
    )


def test_list_active_contract_codes_does_not_read_order_hashes():
    redis_client = Mock()
    redis_client.smembers.return_value = {
        "DCE|JD2609|JD2609",
        "SHFE|AG2612|AG2612",
    }
    index = ActiveOrderIndex(redis_client)

    assert index.list_active_contract_codes() == {
        "JD2609",
        "AG2612",
    }
    redis_client.hgetall.assert_not_called()
