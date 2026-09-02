from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.redis_keys import (
    ACTIVE_ORDERS_ALL_KEY,
    account_active_orders_key,
    active_order_key,
    instrument_active_orders_key,
)
from app.models.account import Account
from app.models.order import Order
from app.repositories.order_repository import OrderRepository
from app.services.active_order_rebuild_service import ActiveOrderRebuildService
from tests.integration.conftest import make_order_service, make_request


pytestmark = pytest.mark.integration


def test_rebuild_restores_repairs_and_removes_active_order_indexes(
    integration_context,
):
    """使用真实PostgreSQL和Redis验证重建、幂等修复及终态清理。"""

    try:
        redis_client.ping()
    except Exception as exc:
        pytest.skip(f"Redis不可用: {exc}")

    order_service = make_order_service(integration_context)
    request = make_request(
        integration_context,
        client_order_id=f"REBUILD-{uuid4().hex[:12]}",
    )
    with SessionLocal() as db:
        order = order_service.create_order(db, request)
        order_id = order.order_id
    with SessionLocal() as db:
        account_cash_before = db.scalar(
            select(Account.available_cash).where(
                Account.account_id == integration_context.account_id
            )
        )

    index = ActiveOrderIndex(redis_client)
    rebuild_service = ActiveOrderRebuildService(
        order_repository=OrderRepository(),
        active_order_index=index,
        batch_size=2,
    )
    ghost_id = f"GHOST-{uuid4().hex.upper()}"
    try:
        # 模拟Redis重启或索引丢失。
        redis_client.delete(active_order_key(order_id))
        redis_client.srem(
            instrument_active_orders_key(
                integration_context.exchange_id,
                integration_context.symbol,
            ),
            order_id,
        )
        redis_client.srem(
            account_active_orders_key(integration_context.account_id),
            order_id,
        )
        redis_client.srem(ACTIVE_ORDERS_ALL_KEY, order_id)

        with SessionLocal() as db:
            first = rebuild_service.rebuild(db)
        detail = index.get_active_order(order_id)
        assert first.upserted >= 1
        assert detail["order_id"] == order_id
        assert detail["limit_price"] == "3500.000000"
        assert order_id in index.list_instrument_order_ids(
            integration_context.exchange_id,
            integration_context.symbol,
        )
        assert order_id in index.list_account_order_ids(
            integration_context.account_id
        )
        assert order_id in index.list_all_order_ids()

        # 损坏Hash并移除Set成员，重复重建必须从数据库快照修复且不重复。
        redis_client.hset(
            active_order_key(order_id),
            mapping={"limit_price": "1.000000"},
        )
        redis_client.srem(
            instrument_active_orders_key(
                integration_context.exchange_id,
                integration_context.symbol,
            ),
            order_id,
        )
        redis_client.srem(
            account_active_orders_key(integration_context.account_id),
            order_id,
        )
        redis_client.srem(ACTIVE_ORDERS_ALL_KEY, order_id)
        with SessionLocal() as db:
            rebuild_service.rebuild(db)
        assert index.get_active_order(order_id)["limit_price"] == "3500.000000"
        assert redis_client.scard(
            instrument_active_orders_key(
                integration_context.exchange_id,
                integration_context.symbol,
            )
        ) == 1
        assert redis_client.scard(
            account_active_orders_key(integration_context.account_id)
        ) == 1

        # PostgreSQL不存在的Redis活动订单应被清理。
        ghost = SimpleNamespace(
            order_id=ghost_id,
            account_id="GHOST-A",
            exchange_id="GHOST-X",
            symbol="GHOST-S",
            order_book_id="GHOST-S",
            instrument_type="FUTURES",
        )
        index.upsert_active_order_for_rebuild(ghost)
        with SessionLocal() as db:
            missing_result = rebuild_service.rebuild(db)
        assert missing_result.removed >= 1
        assert index.get_active_order(ghost_id) == {}
        assert ghost_id not in index.list_all_order_ids()

        # 数据库订单进入终态后，重建应删除它的全部Redis活动索引。
        with SessionLocal() as db:
            stored_order = db.scalar(
                select(Order).where(Order.order_id == order_id)
            )
            stored_order.status = "FILLED"
            stored_order.traded_volume = stored_order.total_volume
            stored_order.remaining_volume = 0
            stored_order.cancelled_volume = 0
            db.commit()
        with SessionLocal() as db:
            terminal_result = rebuild_service.rebuild(db)
        assert terminal_result.removed >= 1
        assert index.get_active_order(order_id) == {}
        assert order_id not in index.list_all_order_ids()

        # 重建不能修改账户资金；订单状态只来自测试显式更新，不由重建改变。
        with SessionLocal() as db:
            account_cash_after = db.scalar(
                select(Account.available_cash).where(
                    Account.account_id == integration_context.account_id
                )
            )
            stored_status = db.scalar(
                select(Order.status).where(Order.order_id == order_id)
            )
        assert account_cash_after == account_cash_before
        assert stored_status == "FILLED"
    finally:
        pipeline = redis_client.pipeline(transaction=True)
        for current_id, account_id, exchange_id, symbol in (
            (
                order_id,
                integration_context.account_id,
                integration_context.exchange_id,
                integration_context.symbol,
            ),
            (ghost_id, "GHOST-A", "GHOST-X", "GHOST-S"),
        ):
            pipeline.delete(active_order_key(current_id))
            pipeline.srem(
                instrument_active_orders_key(exchange_id, symbol),
                current_id,
            )
            pipeline.srem(
                account_active_orders_key(account_id),
                current_id,
            )
            pipeline.srem(ACTIVE_ORDERS_ALL_KEY, current_id)
        pipeline.execute()
