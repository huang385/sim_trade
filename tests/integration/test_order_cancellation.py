from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.common.exceptions import DataAccessError, ResourceConflictError
from app.core.database import SessionLocal
from app.main import app
from app.matching.models import MatchResult
from app.models.account import Account
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.trade import Trade
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.order_schema import OrderCancelRequest
from app.services.trade_settlement_service import (
    SettlementCommand,
    TradeSettlementService,
)
from tests.integration.conftest import (
    make_cancellation_service,
    make_order_service,
    make_request,
)


pytestmark = pytest.mark.integration


def create_order(context, client_order_id, *, volume=10):
    with SessionLocal() as db:
        return make_order_service(context).create_order(
            db,
            make_request(
                context,
                client_order_id=client_order_id,
                volume=volume,
            ),
        )


def cancel_order(order_id, account_id):
    with SessionLocal() as db:
        return make_cancellation_service().cancel_order(
            db=db,
            order_id=order_id,
            request=OrderCancelRequest(account_id=account_id),
        )


def settlement_command(order_id, event_id, volume):
    return SettlementCommand(
        order_id=order_id,
        market_event_id=event_id,
        market_stream_message_id=f"{event_id}-0",
        tick_event_time=datetime(2026, 7, 24, 1, tzinfo=timezone.utc),
        tick_sequence_id=1,
        match_result=MatchResult(
            matched=True,
            fill_price=Decimal("3499"),
            fill_volume=volume,
            reason=None,
            engine_name="VN",
            engine_version="1.0",
        ),
    )


def test_api_cancels_accepted_order_and_repeat_is_idempotent(
    integration_context,
):
    order = create_order(integration_context, "CANCEL-API", volume=10)
    client = TestClient(app)

    first = client.post(
        f"/api/orders/{order.order_id}/cancel",
        json={"account_id": integration_context.account_id},
    )
    second = client.post(
        f"/api/orders/{order.order_id}/cancel",
        json={"account_id": integration_context.account_id},
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == "CANCELLED"
    assert first.json()["cancelled_volume"] == 10
    assert first.json()["remaining_volume"] == 0
    assert first.json()["cancelled_at"] is not None
    assert second.json()["cancelled_at"] == first.json()["cancelled_at"]

    with SessionLocal() as db:
        stored_order = db.scalar(
            select(Order).where(Order.order_id == order.order_id)
        )
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        cancel_events = db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == order.order_id,
                OutboxEvent.event_type == "ORDER_CANCELLED",
            )
        ).all()
        assert stored_order.submit_status == "ACCEPTED"
        assert stored_order.frozen_margin == Decimal("0.000000")
        assert stored_order.frozen_commission == Decimal("0.000000")
        assert account.available_cash == Decimal("100000.000000")
        assert account.frozen_margin == Decimal("0.000000")
        assert account.frozen_commission == Decimal("0.000000")
        assert len(cancel_events) == 1
        assert cancel_events[0].status == "PENDING"


def test_partial_fill_then_cancel_preserves_trade_position_and_used_funds(
    integration_context,
):
    order = create_order(integration_context, "CANCEL-PARTIAL", volume=10)
    with SessionLocal() as db:
        result = TradeSettlementService().settle(
            db,
            settlement_command(order.order_id, "TICK-PARTIAL", 3),
        )
        assert result.action == "SETTLED"

    response = TestClient(app).post(
        f"/api/orders/{order.order_id}/cancel",
        json={"account_id": integration_context.account_id},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "PARTIALLY_CANCELLED"
    assert response.json()["traded_volume"] == 3
    assert response.json()["cancelled_volume"] == 7
    assert response.json()["remaining_volume"] == 0

    with SessionLocal() as db:
        stored_order = db.scalar(
            select(Order).where(Order.order_id == order.order_id)
        )
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        trade = db.scalar(
            select(Trade).where(Trade.order_id == order.order_id)
        )
        position = db.scalar(
            select(Position).where(
                Position.account_id == integration_context.account_id
            )
        )
        detail = db.scalar(
            select(PositionDetail).where(
                PositionDetail.account_id == integration_context.account_id
            )
        )
        assert (
            stored_order.total_volume
            == stored_order.traded_volume
            + stored_order.remaining_volume
            + stored_order.cancelled_volume
        )
        assert stored_order.frozen_margin == Decimal("0.000000")
        assert stored_order.frozen_commission == Decimal("0.000000")
        assert trade.trade_volume == 3
        assert position.total_volume == 3
        assert detail.remaining_volume == 3
        assert account.available_cash == Decimal("87391.000000")
        assert account.frozen_margin == Decimal("0.000000")
        assert account.used_margin == Decimal("12600.000000")
        assert account.frozen_commission == Decimal("0.000000")
        assert account.used_commission == Decimal("9.000000")
        assert account.cash_balance == Decimal("99991.000000")


def test_two_concurrent_cancels_release_funds_once(integration_context):
    order = create_order(integration_context, "CANCEL-CONCURRENT", volume=10)
    barrier = Barrier(2)

    def run_cancel(_index):
        barrier.wait()
        result = cancel_order(order.order_id, integration_context.account_id)
        return result.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(run_cancel, range(2)))

    assert statuses == ["CANCELLED", "CANCELLED"]
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        stored_order = db.scalar(
            select(Order).where(Order.order_id == order.order_id)
        )
        event_count = db.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.aggregate_id == order.order_id,
                OutboxEvent.event_type == "ORDER_CANCELLED",
            )
        )
        assert account.available_cash == Decimal("100000.000000")
        assert account.frozen_margin == Decimal("0.000000")
        assert stored_order.cancelled_volume == 10
        assert event_count == 1


def test_cancel_and_partial_fill_concurrency_preserves_invariants(
    integration_context,
):
    order = create_order(integration_context, "CANCEL-RACE", volume=10)
    barrier = Barrier(2)

    def run_cancel():
        barrier.wait()
        result = cancel_order(order.order_id, integration_context.account_id)
        return result.status

    def run_fill():
        barrier.wait()
        with SessionLocal() as db:
            return TradeSettlementService().settle(
                db,
                settlement_command(order.order_id, "TICK-RACE", 3),
            ).action

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancel_future = executor.submit(run_cancel)
        fill_future = executor.submit(run_fill)
        cancel_status = cancel_future.result()
        fill_action = fill_future.result()

    assert (cancel_status, fill_action) in {
        ("CANCELLED", "ORDER_INACTIVE"),
        ("PARTIALLY_CANCELLED", "SETTLED"),
    }
    with SessionLocal() as db:
        stored_order = db.scalar(
            select(Order).where(Order.order_id == order.order_id)
        )
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        trades = db.scalars(
            select(Trade).where(Trade.order_id == order.order_id)
        ).all()
        assert stored_order.remaining_volume == 0
        assert (
            stored_order.traded_volume + stored_order.cancelled_volume
            == stored_order.total_volume
        )
        assert len(trades) in {0, 1}
        assert sum(item.trade_volume for item in trades) == (
            stored_order.traded_volume
        )
        assert account.frozen_margin >= 0
        assert account.frozen_commission >= 0


def test_cancel_and_full_fill_concurrency_has_one_terminal_winner(
    integration_context,
):
    """全部成交与撤单竞争时，最终只能是全撤或全成，不能重复释放。"""

    order = create_order(integration_context, "CANCEL-FULL-RACE", volume=10)
    barrier = Barrier(2)

    def run_cancel():
        barrier.wait()
        try:
            result = cancel_order(
                order.order_id,
                integration_context.account_id,
            )
            return result.status
        except ResourceConflictError as exc:
            return exc.error_code

    def run_fill():
        barrier.wait()
        with SessionLocal() as db:
            return TradeSettlementService().settle(
                db,
                settlement_command(order.order_id, "TICK-FULL-RACE", 10),
            ).action

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancel_future = executor.submit(run_cancel)
        fill_future = executor.submit(run_fill)
        result_pair = (cancel_future.result(), fill_future.result())

    assert result_pair in {
        ("CANCELLED", "ORDER_INACTIVE"),
        ("ORDER_NOT_CANCELLABLE", "SETTLED"),
    }
    with SessionLocal() as db:
        stored_order = db.scalar(
            select(Order).where(Order.order_id == order.order_id)
        )
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        trades = db.scalars(
            select(Trade).where(Trade.order_id == order.order_id)
        ).all()
        assert stored_order.status in {"CANCELLED", "FILLED"}
        assert stored_order.remaining_volume == 0
        assert (
            stored_order.traded_volume + stored_order.cancelled_volume
            == stored_order.total_volume
        )
        assert sum(item.trade_volume for item in trades) == (
            stored_order.traded_volume
        )
        assert account.frozen_margin == Decimal("0.000000")
        assert account.frozen_commission == Decimal("0.000000")


class FailingCancelOutboxRepository(OutboxRepository):
    @staticmethod
    def create_event(*args, **kwargs):
        raise OperationalError(
            "insert cancel outbox",
            {},
            Exception("failed"),
        )


def test_cancel_outbox_failure_rolls_back_order_and_account(
    integration_context,
):
    order = create_order(integration_context, "CANCEL-ROLLBACK", volume=10)
    service = make_cancellation_service(
        outbox_repository=FailingCancelOutboxRepository()
    )

    with SessionLocal() as db:
        with pytest.raises(DataAccessError):
            service.cancel_order(
                db=db,
                order_id=order.order_id,
                request=OrderCancelRequest(
                    account_id=integration_context.account_id
                ),
            )

    with SessionLocal() as db:
        stored_order = db.scalar(
            select(Order).where(Order.order_id == order.order_id)
        )
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        assert stored_order.status == "ACCEPTED"
        assert stored_order.remaining_volume == 10
        assert stored_order.cancelled_volume == 0
        assert stored_order.cancelled_at is None
        assert stored_order.frozen_margin == Decimal("42000.000000")
        assert account.available_cash == Decimal("57970.000000")
        assert account.frozen_margin == Decimal("42000.000000")
        assert account.frozen_commission == Decimal("30.000000")
        cancel_event_count = db.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.aggregate_id == order.order_id,
                OutboxEvent.event_type.in_(
                    ["ORDER_CANCELLED", "ORDER_PARTIALLY_CANCELLED"]
                ),
            )
        )
        assert cancel_event_count == 0
