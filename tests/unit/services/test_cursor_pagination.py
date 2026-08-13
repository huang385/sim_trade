from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from app.common.exceptions import BusinessValidationError
from app.common.pagination_cursor import decode_cursor, encode_cursor
from app.services.order_service import OrderService
from app.services.trade_settlement_service import TradeQueryService


NOW = datetime(2026, 7, 29, tzinfo=timezone.utc)


def make_order(row_id: int):
    """构造包含OrderResponse全部字段的数据库行替身。"""

    return SimpleNamespace(
        id=row_id,
        order_id=f"O-{row_id}",
        client_order_id=f"C-{row_id}",
        account_id="A001",
        order_book_id="JD2609",
        exchange_id="DCE",
        symbol="JD2609",
        trading_day=date(2026, 7, 29),
        direction="BUY",
        offset_flag="OPEN",
        order_type="LIMIT",
        limit_price=Decimal("3000"),
        total_volume=1,
        traded_volume=0,
        remaining_volume=1,
        cancelled_volume=0,
        average_price=None,
        frozen_margin=Decimal("3000"),
        frozen_commission=Decimal("3"),
        frozen_position_volume=0,
        status="ACCEPTED",
        submit_status="ACCEPTED",
        reject_code=None,
        reject_message=None,
        created_at=NOW,
        accepted_at=NOW,
        cancelled_at=None,
        updated_at=NOW,
    )


def make_trade(row_id: int):
    """构造包含TradeResponse全部字段的数据库行替身。"""

    return SimpleNamespace(
        id=row_id,
        trade_id=f"T-{row_id}",
        order_id=f"O-{row_id}",
        account_id="A001",
        market_event_id=f"M-{row_id}",
        market_stream_message_id=f"{row_id}-0",
        order_book_id="JD2609",
        exchange_id="DCE",
        symbol="JD2609",
        trading_day=date(2026, 7, 29),
        direction="BUY",
        offset_flag="OPEN",
        trade_price=Decimal("3000"),
        trade_volume=1,
        turnover=Decimal("30000"),
        margin=Decimal("3000"),
        commission=Decimal("3"),
        realized_pnl=Decimal("0"),
        daily_close_pnl=Decimal("0"),
        trade_time=NOW,
        created_at=NOW,
    )


def make_order_service(repository):
    return OrderService(
        order_repository=repository,
        account_repository=Mock(),
        rule_query_service=Mock(),
        validation_service=Mock(),
        freeze_service=Mock(),
        margin_calculator=Mock(),
        fee_calculator=Mock(),
    )


def test_cursor_rejects_malformed_mismatched_and_expired_values():
    filters = {"account_id": "A001"}
    with pytest.raises(
        BusinessValidationError,
        match="格式错误",
    ):
        decode_cursor(
            "not-base64!",
            expected_kind="orders",
            expected_filters=filters,
        )

    cursor = encode_cursor(
        kind="orders",
        before_id=10,
        filters=filters,
        now=100,
    )
    with pytest.raises(
        BusinessValidationError,
        match="不匹配",
    ):
        decode_cursor(
            cursor,
            expected_kind="orders",
            expected_filters={"account_id": "B001"},
            now=101,
        )
    with pytest.raises(
        BusinessValidationError,
        match="已过期",
    ):
        decode_cursor(
            cursor,
            expected_kind="orders",
            expected_filters=filters,
            max_age_seconds=10,
            now=111,
        )


def test_order_cursor_pages_have_no_duplicates_and_traverse_all_rows():
    repository = Mock()
    repository.list_page_by_account.side_effect = [
        [make_order(5), make_order(4), make_order(3)],
        [make_order(3), make_order(2), make_order(1)],
        [make_order(1)],
    ]
    service = make_order_service(repository)
    first_db = Mock()
    second_db = Mock()
    third_db = Mock()

    first = service.list_order_page(
        first_db,
        "A001",
        cursor=None,
        limit=2,
    )
    second = service.list_order_page(
        second_db,
        "A001",
        cursor=first.next_cursor,
        limit=2,
    )
    third = service.list_order_page(
        third_db,
        "A001",
        cursor=second.next_cursor,
        limit=2,
    )

    assert [item.order_id for item in first.items] == ["O-5", "O-4"]
    assert [item.order_id for item in second.items] == ["O-3", "O-2"]
    assert [item.order_id for item in third.items] == ["O-1"]
    assert first.has_more is second.has_more is True
    assert third.has_more is False
    assert third.next_cursor is None
    all_ids = [
        item.order_id
        for page in (first, second, third)
        for item in page.items
    ]
    assert len(all_ids) == len(set(all_ids)) == 5
    assert repository.list_page_by_account.call_args_list == [
        call(
                first_db,
                "A001",
                trading_day=None,
                before_id=None,
            fetch_size=3,
        ),
        call(
                second_db,
                "A001",
                trading_day=None,
                before_id=4,
            fetch_size=3,
        ),
        call(
                third_db,
                "A001",
                trading_day=None,
                before_id=2,
            fetch_size=3,
        ),
    ]


def test_empty_order_page_has_no_cursor():
    repository = Mock()
    repository.list_page_by_account.return_value = []
    page = make_order_service(repository).list_order_page(
        Mock(),
        "A001",
        cursor=None,
        limit=100,
    )

    assert page.items == []
    assert page.has_more is False
    assert page.next_cursor is None


def test_trade_page_cursor_is_bound_to_filters_and_continues():
    repository = Mock()
    repository.list_page.side_effect = [
        [make_trade(3), make_trade(2), make_trade(1)],
        [make_trade(1)],
    ]
    service = TradeQueryService(repository=repository)

    first = service.list_page(
        Mock(),
        account_id="A001",
        order_id=None,
        cursor=None,
        limit=2,
    )
    second = service.list_page(
        Mock(),
        account_id="A001",
        order_id=None,
        cursor=first.next_cursor,
        limit=2,
    )

    assert [item.trade_id for item in first.items] == ["T-3", "T-2"]
    assert [item.trade_id for item in second.items] == ["T-1"]
    assert second.next_cursor is None
    with pytest.raises(BusinessValidationError, match="不匹配"):
        service.list_page(
            Mock(),
            account_id="B001",
            order_id=None,
            cursor=first.next_cursor,
            limit=2,
        )
