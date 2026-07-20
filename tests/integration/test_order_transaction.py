from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.common.exceptions import DataAccessError
from app.core.database import SessionLocal
from app.models.account import Account
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.repositories.outbox_repository import OutboxRepository
from tests.integration.conftest import make_order_service, make_request


pytestmark = pytest.mark.integration


def test_order_freeze_and_outbox_commit_in_one_transaction(integration_context):
    service = make_order_service(integration_context)
    request = make_request(
        integration_context,
        client_order_id="TX-SUCCESS",
    )

    with SessionLocal() as db:
        order = service.create_order(db, request)
        order_id = order.order_id

    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        stored_order = db.scalar(
            select(Order).where(Order.order_id == order_id)
        )
        event = db.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == order_id
            )
        )

        assert stored_order.status == "ACCEPTED"
        assert stored_order.frozen_margin == Decimal("8400.000000")
        assert account.available_cash == Decimal("91594.000000")
        assert account.frozen_margin == Decimal("8400.000000")
        assert account.frozen_commission == Decimal("6.000000")
        assert account.cash_balance == Decimal("100000.000000")
        assert account.used_margin == Decimal("0.000000")
        assert account.used_commission == Decimal("0.000000")
        assert event.status == "PENDING"
        assert event.event_type == "ORDER_ACCEPTED"
        assert event.payload["limit_price"] == "3500.000000"


class FailingOutboxRepository(OutboxRepository):
    @staticmethod
    def create_event(*args, **kwargs):
        raise OperationalError("insert outbox", {}, Exception("failed"))


def test_outbox_failure_rolls_back_order_and_freeze(integration_context):
    service = make_order_service(
        integration_context,
        outbox_repository=FailingOutboxRepository(),
    )
    request = make_request(
        integration_context,
        client_order_id="TX-ROLLBACK",
    )

    with SessionLocal() as db:
        with pytest.raises(DataAccessError):
            service.create_order(db, request)

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
        assert order_count == 0
        assert account.available_cash == Decimal("100000.000000")
        assert account.frozen_margin == Decimal("0.000000")
        assert account.frozen_commission == Decimal("0.000000")


def test_database_rejects_unbalanced_order_volumes(integration_context):
    """数据库约束必须拒绝不满足总量平衡关系的订单数量。"""

    service = make_order_service(integration_context)
    request = make_request(
        integration_context,
        client_order_id="TX-VOLUME-BALANCE",
    )
    with SessionLocal() as db:
        order = service.create_order(db, request)
        order_id = order.order_id

    with SessionLocal() as db:
        order = db.scalar(select(Order).where(Order.order_id == order_id))
        order.cancelled_volume = 1
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    with SessionLocal() as db:
        order = db.scalar(select(Order).where(Order.order_id == order_id))
        assert order.total_volume == (
            order.traded_volume
            + order.remaining_volume
            + order.cancelled_volume
        )
