from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models.account import Account
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from tests.integration.conftest import make_order_service, make_request


pytestmark = pytest.mark.integration


def test_duplicate_client_order_id_freezes_and_emits_once(integration_context):
    service = make_order_service(integration_context)
    request = make_request(
        integration_context,
        client_order_id="IDEMPOTENT-1",
    )

    with SessionLocal() as first_db:
        first = service.create_order(first_db, request)
        first_order_id = first.order_id
    with SessionLocal() as second_db:
        second = service.create_order(second_db, request)

    assert second.order_id == first_order_id
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
        event_count = db.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.aggregate_id == first_order_id
            )
        )
        assert order_count == 1
        assert event_count == 1
        assert account.available_cash == Decimal("91594.000000")
        assert account.frozen_margin == Decimal("8400.000000")
        assert account.frozen_commission == Decimal("6.000000")
