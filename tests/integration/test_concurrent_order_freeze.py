from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.common.exceptions import BusinessRuleError
from app.core.database import SessionLocal
from app.models.account import Account
from app.models.order import Order
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
                return ("accepted", service.create_order(db, request).order_id)
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
