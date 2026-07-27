from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.common.exceptions import BusinessRuleError, DataAccessError
from app.core.database import SessionLocal
from app.matching.models import MatchResult
from app.models.account import Account
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.position_freeze_allocation import PositionFreezeAllocation
from app.models.trade import Trade
from app.models.trade_position_allocation import TradePositionAllocation
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.order_schema import OrderCancelRequest
from app.services.order_cancellation_service import OrderCancellationService
from app.services.trade_settlement_service import (
    SettlementCommand,
    TradeSettlementService,
)
from tests.integration.conftest import make_order_service, make_request


pytestmark = pytest.mark.integration


def settle(order_id, event_id, price, volume):
    with SessionLocal() as db:
        return TradeSettlementService().settle(
            db,
            SettlementCommand(
                order_id=order_id,
                market_event_id=event_id,
                market_stream_message_id=f"{event_id}-0",
                tick_event_time=datetime(
                    2026, 7, 24, 1, tzinfo=timezone.utc
                ),
                tick_sequence_id=1,
                match_result=MatchResult(
                    matched=True,
                    fill_price=Decimal(price),
                    fill_volume=volume,
                    reason=None,
                    engine_name="VN",
                    engine_version="1.0",
                ),
            ),
        )


def create_open_position(context, *, direction="BUY", volume=10):
    with SessionLocal() as db:
        order = make_order_service(context).create_order(
            db,
            make_request(
                context,
                client_order_id=f"OPEN-{uuid4().hex}",
                direction=direction,
                volume=volume,
            ),
        )
        order_id = order.order_id
    assert settle(order_id, f"TICK-OPEN-{uuid4().hex}", "3500", volume).action == (
        "SETTLED"
    )
    return order_id


def create_close_order(
    context,
    *,
    client_order_id,
    direction="SELL",
    offset_flag="CLOSE_TODAY",
    volume=4,
    outbox_repository=None,
):
    with SessionLocal() as db:
        return make_order_service(
            context,
            outbox_repository=outbox_repository,
        ).create_order(
            db,
            make_request(
                context,
                client_order_id=client_order_id,
                direction=direction,
                offset_flag=offset_flag,
                limit_price=Decimal("3520"),
                volume=volume,
            ),
        )


def test_close_today_partial_fill_then_cancel_complete_chain(
    integration_context,
):
    create_open_position(integration_context)
    close_order = create_close_order(
        integration_context,
        client_order_id="CLOSE-LIFECYCLE",
    )

    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
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
        allocation = db.scalar(
            select(PositionFreezeAllocation).where(
                PositionFreezeAllocation.order_id == close_order.order_id
            )
        )
        assert close_order.frozen_margin == Decimal("0.000000")
        assert close_order.frozen_commission == Decimal("24.000000")
        assert close_order.frozen_position_volume == 4
        assert account.available_cash == Decimal("57946.000000")
        assert account.frozen_commission == Decimal("24.000000")
        assert position.total_volume == 10
        assert (position.frozen_volume, position.available_volume) == (4, 6)
        assert detail.frozen_volume == 4
        assert allocation.remaining_frozen_volume == 4

    assert settle(
        close_order.order_id,
        "TICK-CLOSE-PARTIAL",
        "3522",
        3,
    ).action == "SETTLED"

    with SessionLocal() as db:
        OrderCancellationService().cancel_order(
            db=db,
            order_id=close_order.order_id,
            request=OrderCancelRequest(
                account_id=integration_context.account_id
            ),
        )

    with SessionLocal() as db:
        order = db.scalar(
            select(Order).where(Order.order_id == close_order.order_id)
        )
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
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
        allocation = db.scalar(
            select(PositionFreezeAllocation).where(
                PositionFreezeAllocation.order_id == close_order.order_id
            )
        )
        close_trade = db.scalar(
            select(Trade).where(Trade.order_id == close_order.order_id)
        )
        trade_allocations = db.scalars(
            select(TradePositionAllocation).where(
                TradePositionAllocation.trade_id == close_trade.trade_id
            )
        ).all()
        assert order.status == "PARTIALLY_CANCELLED"
        assert (
            order.traded_volume,
            order.remaining_volume,
            order.cancelled_volume,
        ) == (3, 0, 1)
        assert order.frozen_position_volume == 0
        assert close_trade.realized_pnl == Decimal("660.000000")
        assert close_trade.margin == Decimal("12600.000000")
        assert account.used_margin == Decimal("29400.000000")
        assert account.realized_pnl == Decimal("660.000000")
        assert account.cash_balance == Decimal("100612.000000")
        assert account.available_cash == Decimal("71212.000000")
        assert account.daily_pnl == Decimal("612.000000")
        assert (position.total_volume, position.frozen_volume) == (7, 0)
        assert position.available_volume == 7
        assert detail.remaining_volume == 7
        assert detail.frozen_volume == 0
        assert detail.remaining_margin == Decimal("29400.000000")
        assert allocation.consumed_volume == 3
        assert allocation.released_volume == 1
        assert allocation.status == "RELEASED"
        assert len(trade_allocations) == 1
        assert trade_allocations[0].close_volume == 3
        assert trade_allocations[0].commission == close_trade.commission


def test_plain_close_crosses_yesterday_and_today_with_distinct_fees(
    integration_context,
):
    """真实 PostgreSQL 验证普通 CLOSE 的今昨拆分和成交明细关系。"""

    create_open_position(integration_context, volume=5)
    create_open_position(integration_context, volume=5)
    with SessionLocal() as db:
        position = db.scalar(
            select(Position).where(
                Position.account_id == integration_context.account_id
            )
        )
        details = db.scalars(
            select(PositionDetail)
            .where(
                PositionDetail.account_id
                == integration_context.account_id
            )
            .order_by(PositionDetail.id)
        ).all()
        details[0].open_trading_day = (
            integration_context.trading_day - timedelta(days=1)
        )
        position.today_volume = 5
        position.yesterday_volume = 5
        db.commit()

    close_order = create_close_order(
        integration_context,
        client_order_id="CLOSE-CROSS-DAY",
        offset_flag="CLOSE",
        volume=10,
    )
    with SessionLocal() as db:
        allocations = db.scalars(
            select(PositionFreezeAllocation)
            .where(
                PositionFreezeAllocation.order_id
                == close_order.order_id
            )
            .order_by(PositionFreezeAllocation.id)
        ).all()
        assert close_order.frozen_commission == Decimal("45.000000")
        assert [
            (
                item.resolved_offset_flag,
                item.remaining_frozen_commission,
            )
            for item in allocations
        ] == [
            ("CLOSE_YESTERDAY", Decimal("15.000000")),
            ("CLOSE_TODAY", Decimal("30.000000")),
        ]

    assert settle(
        close_order.order_id,
        "TICK-CLOSE-CROSS-DAY",
        "3522",
        7,
    ).action == "SETTLED"
    with SessionLocal() as db:
        trade = db.scalar(
            select(Trade).where(Trade.order_id == close_order.order_id)
        )
        details = db.scalars(
            select(TradePositionAllocation)
            .where(TradePositionAllocation.trade_id == trade.trade_id)
            .order_by(TradePositionAllocation.id)
        ).all()
        assert trade.commission == Decimal("27.000000")
        assert [
            (item.resolved_offset_flag, item.close_volume, item.commission)
            for item in details
        ] == [
            ("CLOSE_YESTERDAY", 5, Decimal("15.000000")),
            ("CLOSE_TODAY", 2, Decimal("12.000000")),
        ]
        assert sum(item.close_volume for item in details) == trade.trade_volume
        assert sum(item.released_margin for item in details) == trade.margin
        assert sum(item.realized_pnl for item in details) == trade.realized_pnl
        assert sum(item.commission for item in details) == trade.commission


def test_close_today_cannot_use_yesterday_position(integration_context):
    create_open_position(integration_context)
    with SessionLocal() as db:
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
        position.today_volume = 0
        position.yesterday_volume = position.total_volume
        detail.open_trading_day = (
            integration_context.trading_day - timedelta(days=1)
        )
        db.commit()

    with pytest.raises(BusinessRuleError) as exc_info:
        create_close_order(
            integration_context,
            client_order_id="CLOSE-TODAY-NO-FALLBACK",
            offset_flag="CLOSE_TODAY",
        )
    assert exc_info.value.error_code == "INSUFFICIENT_TODAY_POSITION"

    close_order = create_close_order(
        integration_context,
        client_order_id="CLOSE-YESTERDAY",
        offset_flag="CLOSE_YESTERDAY",
    )
    assert close_order.status == "ACCEPTED"
    assert close_order.frozen_position_volume == 4


def test_concurrent_close_orders_cannot_double_freeze(integration_context):
    create_open_position(integration_context)
    barrier = Barrier(2)

    def submit(index):
        barrier.wait()
        try:
            order = create_close_order(
                integration_context,
                client_order_id=f"CLOSE-CONCURRENT-{index}",
                volume=8,
            )
            return order.status
        except BusinessRuleError as exc:
            return exc.error_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, range(2)))

    assert sorted(results) == ["ACCEPTED", "INSUFFICIENT_TODAY_POSITION"]
    with SessionLocal() as db:
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
        frozen = db.scalar(
            select(
                func.sum(
                    PositionFreezeAllocation.remaining_frozen_volume
                )
            ).where(
                PositionFreezeAllocation.account_id
                == integration_context.account_id
            )
        )
        assert position.frozen_volume == detail.frozen_volume == frozen == 8
        assert position.available_volume == 2


def test_close_fill_and_cancel_concurrency_has_one_legal_result(
    integration_context,
):
    create_open_position(integration_context)
    close_order = create_close_order(
        integration_context,
        client_order_id="CLOSE-RACE",
        volume=4,
    )
    barrier = Barrier(2)

    def run_cancel():
        barrier.wait()
        with SessionLocal() as db:
            return OrderCancellationService().cancel_order(
                db=db,
                order_id=close_order.order_id,
                request=OrderCancelRequest(
                    account_id=integration_context.account_id
                ),
            ).status

    def run_fill():
        barrier.wait()
        return settle(
            close_order.order_id,
            "TICK-CLOSE-RACE",
            "3522",
            3,
        ).action

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancel_future = executor.submit(run_cancel)
        fill_future = executor.submit(run_fill)
        pair = (cancel_future.result(), fill_future.result())

    assert pair in {
        ("CANCELLED", "ORDER_INACTIVE"),
        ("PARTIALLY_CANCELLED", "SETTLED"),
    }
    with SessionLocal() as db:
        order = db.scalar(
            select(Order).where(Order.order_id == close_order.order_id)
        )
        position = db.scalar(
            select(Position).where(
                Position.account_id == integration_context.account_id
            )
        )
        allocations = db.scalars(
            select(PositionFreezeAllocation).where(
                PositionFreezeAllocation.order_id == close_order.order_id
            )
        ).all()
        assert order.remaining_volume == 0
        assert order.traded_volume + order.cancelled_volume == 4
        assert order.frozen_position_volume == 0
        assert position.frozen_volume == 0
        assert sum(
            item.remaining_frozen_volume for item in allocations
        ) == 0


def test_cancel_one_close_order_does_not_release_another_order(
    integration_context,
):
    create_open_position(integration_context)
    first = create_close_order(
        integration_context,
        client_order_id="CLOSE-ISOLATED-1",
        volume=4,
    )
    second = create_close_order(
        integration_context,
        client_order_id="CLOSE-ISOLATED-2",
        volume=3,
    )
    with SessionLocal() as db:
        OrderCancellationService().cancel_order(
            db=db,
            order_id=first.order_id,
            request=OrderCancelRequest(
                account_id=integration_context.account_id
            ),
        )

    with SessionLocal() as db:
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
        first_allocation = db.scalar(
            select(PositionFreezeAllocation).where(
                PositionFreezeAllocation.order_id == first.order_id
            )
        )
        second_allocation = db.scalar(
            select(PositionFreezeAllocation).where(
                PositionFreezeAllocation.order_id == second.order_id
            )
        )
        assert position.frozen_volume == 3
        assert position.available_volume == 7
        assert detail.frozen_volume == 3
        assert first_allocation.released_volume == 4
        assert first_allocation.remaining_frozen_volume == 0
        assert second_allocation.released_volume == 0
        assert second_allocation.remaining_frozen_volume == 3


def test_concurrent_ticks_cannot_overfill_close_order(integration_context):
    create_open_position(integration_context)
    close_order = create_close_order(
        integration_context,
        client_order_id="CLOSE-TICK-RACE",
        volume=4,
    )
    barrier = Barrier(2)

    def run_fill(index):
        barrier.wait()
        return settle(
            close_order.order_id,
            f"TICK-CLOSE-CONCURRENT-{index}",
            "3522",
            3,
        ).action

    with ThreadPoolExecutor(max_workers=2) as executor:
        actions = list(executor.map(run_fill, range(2)))

    assert actions == ["SETTLED", "SETTLED"]
    with SessionLocal() as db:
        order = db.scalar(
            select(Order).where(Order.order_id == close_order.order_id)
        )
        trades = db.scalars(
            select(Trade).where(Trade.order_id == close_order.order_id)
        ).all()
        position = db.scalar(
            select(Position).where(
                Position.account_id == integration_context.account_id
            )
        )
        allocation = db.scalar(
            select(PositionFreezeAllocation).where(
                PositionFreezeAllocation.order_id == close_order.order_id
            )
        )
        assert order.status == "FILLED"
        assert order.traded_volume == 4
        assert order.remaining_volume == 0
        assert sorted(item.trade_volume for item in trades) == [1, 3]
        assert position.total_volume == 6
        assert position.frozen_volume == 0
        assert allocation.consumed_volume == 4
        assert allocation.remaining_frozen_volume == 0


class FailingCloseOutbox(OutboxRepository):
    @staticmethod
    def create_event(*args, **kwargs):
        raise OperationalError(
            "insert close outbox",
            {},
            Exception("failed"),
        )


def test_close_order_outbox_failure_rolls_back_all_freezes(
    integration_context,
):
    create_open_position(integration_context)
    with pytest.raises(DataAccessError):
        create_close_order(
            integration_context,
            client_order_id="CLOSE-ROLLBACK",
            outbox_repository=FailingCloseOutbox(),
        )

    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
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
        allocations = db.scalars(
            select(PositionFreezeAllocation).where(
                PositionFreezeAllocation.account_id
                == integration_context.account_id
            )
        ).all()
        failed_order = db.scalar(
            select(Order).where(
                Order.client_order_id == "CLOSE-ROLLBACK"
            )
        )
        assert failed_order is None
        assert account.available_cash == Decimal("57970.000000")
        assert account.frozen_commission == Decimal("0.000000")
        assert position.frozen_volume == 0
        assert position.available_volume == 10
        assert detail.frozen_volume == 0
        assert allocations == []
