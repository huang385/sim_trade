from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.common.exceptions import BusinessRuleError, ResourceConflictError
from app.core.database import SessionLocal
from app.models.account import Account
from app.models.order import Order
from app.services.account_access_scope import AccountAccessScope
from tests.integration.conftest import make_order_service, make_request


pytestmark = pytest.mark.integration


def test_concurrent_orders_cannot_reuse_same_available_cash(integration_context):
    # 单笔订单需要 8406；账户只保留 10000，因此并发请求最多成功一笔。
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        account.initial_cash = Decimal("10000")
        account.cash_balance = Decimal("10000")
        account.available_cash = Decimal("10000")
        account.equity = Decimal("10000")
        db.commit()

    def submit(client_order_id):
        service = make_order_service(integration_context)
        request = make_request(
            integration_context,
            client_order_id=client_order_id,
        )
        try:
            with SessionLocal() as db:
                return (
                    "accepted",
                    service.create_order(
                        db,
                        request,
                        access_scope=AccountAccessScope.for_user(
                            integration_context.user_id
                        ),
                    ).order_id,
                )
        except BusinessRuleError as exc:
            return ("rejected", exc.error_code)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(submit, ["CONCURRENT-1", "CONCURRENT-2"])
        )

    assert sorted(result[0] for result in results) == ["accepted", "rejected"]
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        order_count = db.scalar(
            select(func.count(Order.id)).where(
                Order.account_id == integration_context.account_id
            )
        )
        assert order_count == 1
        assert account.available_cash == Decimal("1594.000000")
        assert account.frozen_margin == Decimal("8400.000000")
        assert account.frozen_commission == Decimal("6.000000")


def test_concurrent_same_client_order_id_freezes_only_once(
    integration_context,
):
    """两个同幂等键请求可以都成功，但必须返回同一订单且只冻结一次。"""

    def submit(_index):
        service = make_order_service(integration_context)
        request = make_request(
            integration_context,
            client_order_id="CONCURRENT-SAME-ID",
        )
        with SessionLocal() as db:
            return service.create_order(
                db,
                request,
                access_scope=AccountAccessScope.for_user(
                    integration_context.user_id
                ),
            ).order_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        order_ids = list(executor.map(submit, range(2)))

    assert len(set(order_ids)) == 1
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        order_count = db.scalar(
            select(func.count(Order.id)).where(
                Order.account_id == integration_context.account_id,
                Order.client_order_id == "CONCURRENT-SAME-ID",
            )
        )
        assert order_count == 1
        assert account.available_cash == Decimal("91594.000000")
        assert account.frozen_margin == Decimal("8400.000000")
        assert account.frozen_commission == Decimal("6.000000")


def test_concurrent_same_client_id_with_different_content_conflicts(
    integration_context,
):
    """相同幂等键的不同业务内容只能有一个成功，另一请求必须返回冲突。"""

    def submit(direction):
        request = make_request(
            integration_context,
            client_order_id="CONCURRENT-DIFFERENT-CONTENT",
            direction=direction,
        )
        try:
            with SessionLocal() as db:
                order = make_order_service(
                    integration_context
                ).create_order(
                    db,
                    request,
                    access_scope=AccountAccessScope.for_user(
                        integration_context.user_id
                    ),
                )
                return ("accepted", order.order_id)
        except ResourceConflictError as exc:
            return ("conflict", exc.error_code)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ["BUY", "SELL"]))

    assert sorted(item[0] for item in results) == ["accepted", "conflict"]
    assert next(
        item[1] for item in results if item[0] == "conflict"
    ) == "IDEMPOTENCY_KEY_REUSED"
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        orders = list(
            db.scalars(
                select(Order).where(
                    Order.account_id == integration_context.account_id,
                    Order.client_order_id
                    == "CONCURRENT-DIFFERENT-CONTENT",
                )
            )
        )
        assert len(orders) == 1
        expected_margin = (
            Decimal("8400.000000")
            if orders[0].direction == "BUY"
            else Decimal("9100.000000")
        )
        assert account.frozen_margin == expected_margin
        assert account.frozen_commission == Decimal("6.000000")
        assert account.available_cash == (
            Decimal("100000.000000")
            - expected_margin
            - Decimal("6.000000")
        )
