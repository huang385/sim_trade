from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models.trade import Trade
from tests.integration.conftest import make_order_service, make_request


def test_order_cursor_pagination_is_stable_during_concurrent_insert(
    integration_context,
):
    """真实PostgreSQL验证订单分页无重复、无遗漏且不混入翻页期间新订单。"""

    service = make_order_service(integration_context)
    original_ids: list[str] = []
    with SessionLocal() as db:
        for index in range(5):
            order = service.create_order(
                db,
                make_request(
                    integration_context,
                    client_order_id=f"PAGE-ORDER-{index}",
                    volume=1,
                ),
            )
            original_ids.append(order.order_id)

    client = TestClient(app)
    first = client.get(
        "/api/orders/page",
        params={
            "account_id": integration_context.account_id,
            "limit": 2,
        },
    )
    assert first.status_code == 200
    first_page = first.json()
    assert first_page["has_more"] is True
    assert first_page["next_cursor"]

    # 第一页之后插入更大的主键；继续旧游标时必须保持向历史方向遍历，
    # 不能把新订单插进第二页造成重复或遗漏。
    with SessionLocal() as db:
        concurrent = service.create_order(
            db,
            make_request(
                integration_context,
                client_order_id="PAGE-ORDER-CONCURRENT",
                volume=1,
            ),
        )

    collected = [
        item["order_id"] for item in first_page["items"]
    ]
    cursor = first_page["next_cursor"]
    while cursor:
        response = client.get(
            "/api/orders/page",
            params={
                "account_id": integration_context.account_id,
                "limit": 2,
                "cursor": cursor,
            },
        )
        assert response.status_code == 200
        page = response.json()
        collected.extend(
            item["order_id"] for item in page["items"]
        )
        cursor = page["next_cursor"]

    assert len(collected) == len(set(collected)) == 5
    assert set(collected) == set(original_ids)
    assert concurrent.order_id not in collected

    invalid = client.get(
        "/api/orders/page",
        params={
            "account_id": integration_context.account_id,
            "cursor": "invalid-cursor",
        },
    )
    assert invalid.status_code == 400
    assert invalid.json()["error_code"] == "INVALID_CURSOR"

    too_large = client.get(
        "/api/orders/page",
        params={
            "account_id": integration_context.account_id,
            "limit": 501,
        },
    )
    assert too_large.status_code == 422


def test_trade_cursor_pagination_traverses_complete_filtered_history(
    integration_context,
):
    """真实PostgreSQL验证成交分页游标可被客户端连续使用。"""

    suffix = uuid4().hex[:10].upper()
    with SessionLocal() as db:
        for index in range(5):
            db.add(
                Trade(
                    trade_id=f"TPAGE-{suffix}-{index}",
                    order_id=f"OPAGE-{suffix}-{index}",
                    account_id=integration_context.account_id,
                    market_event_id=f"MPAGE-{suffix}-{index}",
                    market_stream_message_id=f"{index + 1}-0",
                    order_book_id=integration_context.symbol,
                    exchange_id=integration_context.exchange_id,
                    symbol=integration_context.symbol,
                    trading_day=integration_context.trading_day,
                    direction="BUY",
                    offset_flag="OPEN",
                    trade_price=Decimal("3500"),
                    trade_volume=1,
                    turnover=Decimal("35000"),
                    margin=Decimal("4200"),
                    commission=Decimal("3"),
                    realized_pnl=Decimal("0"),
                    daily_close_pnl=Decimal("0"),
                    trade_time=datetime.now(timezone.utc),
                    created_at=datetime.now(timezone.utc),
                )
            )
        db.commit()

    client = TestClient(app)
    collected: list[str] = []
    cursor = None
    while True:
        params = {
            "account_id": integration_context.account_id,
            "limit": 2,
        }
        if cursor:
            params["cursor"] = cursor
        response = client.get("/api/trades/page", params=params)
        assert response.status_code == 200
        page = response.json()
        collected.extend(
            item["trade_id"] for item in page["items"]
        )
        cursor = page["next_cursor"]
        if cursor is None:
            assert page["has_more"] is False
            break

    assert len(collected) == len(set(collected)) == 5
