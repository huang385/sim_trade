from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.api.trade_api import get_trade_query_service
from app.common.exceptions import ResourceNotFoundError
from app.core.database import get_db
from app.main import app


NOW = datetime(2026, 7, 27, 1, tzinfo=timezone.utc)
TRADING_DAY = date(2026, 7, 27)


def make_allocation(index: int, resolved_offset_flag: str):
    """构造API响应所需的平仓成交逐笔审计对象。"""

    return SimpleNamespace(
        trade_position_allocation_id=f"TPA-{index}",
        trade_id="T-CLOSE",
        order_id="O-CLOSE",
        allocation_id=f"PFA-{index}",
        position_id="P-1",
        position_detail_id=f"PD-{index}",
        account_id="A001",
        order_book_id="AG2612",
        exchange_id="SHFE",
        symbol="AG2612",
        resolved_offset_flag=resolved_offset_flag,
        open_trading_day=(
            TRADING_DAY
            if resolved_offset_flag == "CLOSE_TODAY"
            else date(2026, 7, 26)
        ),
        close_trading_day=TRADING_DAY,
        open_price=Decimal("3500.000000"),
        close_price=Decimal("3520.000000"),
        close_volume=1,
        released_margin=Decimal("4200.000000"),
        commission=Decimal("0.035200"),
        realized_pnl=Decimal("200.000000"),
        created_at=NOW,
    )


@pytest.fixture
def trade_allocation_api():
    service = Mock()
    database_session = Mock()

    def override_db():
        yield database_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_trade_query_service] = lambda: service
    try:
        yield TestClient(app), service, database_session
    finally:
        app.dependency_overrides.clear()


def test_close_trade_returns_cross_day_position_allocations(
    trade_allocation_api,
):
    """一笔普通CLOSE跨今昨仓时返回两条且标志准确。"""

    client, service, database_session = trade_allocation_api
    service.list_position_allocations.return_value = [
        make_allocation(1, "CLOSE_YESTERDAY"),
        make_allocation(2, "CLOSE_TODAY"),
    ]

    response = client.get(
        "/api/trades/T-CLOSE/position-allocations"
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert [item["position_detail_id"] for item in body] == [
        "PD-1",
        "PD-2",
    ]
    assert [item["resolved_offset_flag"] for item in body] == [
        "CLOSE_YESTERDAY",
        "CLOSE_TODAY",
    ]
    # Decimal由Pydantic按字符串输出，API链路不引入二进制float。
    assert body[0]["open_price"] == "3500.000000"
    assert body[0]["commission"] == "0.035200"
    service.list_position_allocations.assert_called_once_with(
        database_session,
        "T-CLOSE",
    )


def test_open_trade_returns_empty_position_allocations(
    trade_allocation_api,
):
    """开仓Trade存在但没有关闭任何持仓，因此返回空列表。"""

    client, service, _ = trade_allocation_api
    service.list_position_allocations.return_value = []

    response = client.get(
        "/api/trades/T-OPEN/position-allocations"
    )

    assert response.status_code == 200
    assert response.json() == []


def test_missing_trade_returns_trade_not_found(trade_allocation_api):
    client, service, _ = trade_allocation_api
    service.list_position_allocations.side_effect = ResourceNotFoundError(
        "成交不存在",
        error_code="TRADE_NOT_FOUND",
    )

    response = client.get(
        "/api/trades/T-MISSING/position-allocations"
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "TRADE_NOT_FOUND"
