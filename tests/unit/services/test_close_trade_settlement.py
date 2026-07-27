from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.matching.models import MatchResult
from app.models.account import Account
from app.models.instrument import Instrument
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.position_freeze_allocation import PositionFreezeAllocation
from app.models.trade import Trade
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.order_schema import OrderCancelRequest
from app.services.order_cancellation_service import OrderCancellationService
from app.services.trade_settlement_service import (
    SettlementCommand,
    TradeSettlementService,
)


TRADING_DAY = date(2026, 7, 24)
NOW = datetime(2026, 7, 24, 1, tzinfo=timezone.utc)


def factory():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def seed_close(
    session_factory,
    *,
    close_direction="SELL",
    order_volume=4,
    position_volume=10,
    open_margin=Decimal("42000"),
):
    position_direction = "LONG" if close_direction == "SELL" else "SHORT"
    frozen_commission = Decimal(order_volume) * Decimal("6")
    with session_factory() as db:
        db.add(
            Instrument(
                order_book_id="AG2609",
                symbol="AG2609",
                exchange_id="SHFE",
                contract_multiplier=Decimal("10"),
                price_tick=Decimal("1"),
                min_volume=1,
                max_volume=100,
                is_active=True,
            )
        )
        db.add(
            Account(
                account_id="A001",
                account_name="test",
                initial_cash=Decimal("100000"),
                cash_balance=Decimal("99970"),
                available_cash=Decimal("57970") - frozen_commission,
                frozen_cash=Decimal("0"),
                equity=Decimal("99970"),
                used_margin=open_margin,
                frozen_margin=Decimal("0"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                daily_pnl=Decimal("-30"),
                used_commission=Decimal("30"),
                frozen_commission=frozen_commission,
                risk_ratio=Decimal("0"),
                status="NORMAL",
                trading_day=TRADING_DAY,
            )
        )
        db.add(
            Order(
                order_id="O-CLOSE",
                client_order_id="C-CLOSE",
                account_id="A001",
                order_book_id="AG2609",
                symbol="AG2609",
                exchange_id="SHFE",
                trading_day=TRADING_DAY,
                direction=close_direction,
                offset_flag="CLOSE_TODAY",
                order_type="LIMIT",
                limit_price=Decimal("3520"),
                total_volume=order_volume,
                traded_volume=0,
                remaining_volume=order_volume,
                cancelled_volume=0,
                average_price=None,
                status="ACCEPTED",
                submit_status="ACCEPTED",
                frozen_margin=Decimal("0"),
                frozen_commission=frozen_commission,
                frozen_position_volume=order_volume,
                created_at=NOW,
                accepted_at=NOW,
                updated_at=NOW,
            )
        )
        db.add(
            Position(
                position_id="P-1",
                account_id="A001",
                order_book_id="AG2609",
                exchange_id="SHFE",
                symbol="AG2609",
                direction=position_direction,
                total_volume=position_volume,
                today_volume=position_volume,
                yesterday_volume=0,
                frozen_volume=order_volume,
                available_volume=position_volume - order_volume,
                average_open_price=Decimal("3500"),
                position_cost=Decimal(position_volume) * Decimal("35000"),
                used_margin=open_margin,
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                trading_day=TRADING_DAY,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        db.add(
            PositionDetail(
                position_detail_id="PD-1",
                position_id="P-1",
                account_id="A001",
                open_trade_id="T-OPEN",
                order_book_id="AG2609",
                exchange_id="SHFE",
                symbol="AG2609",
                direction=position_direction,
                open_trading_day=TRADING_DAY,
                open_price=Decimal("3500"),
                original_volume=position_volume,
                remaining_volume=position_volume,
                frozen_volume=order_volume,
                open_margin=open_margin,
                remaining_margin=open_margin,
                open_commission=Decimal("30"),
                status="OPEN",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        db.add(
            PositionFreezeAllocation(
                allocation_id="PFA-1",
                order_id="O-CLOSE",
                position_id="P-1",
                position_detail_id="PD-1",
                account_id="A001",
                exchange_id="SHFE",
                symbol="AG2609",
                offset_flag="CLOSE_TODAY",
                original_frozen_volume=order_volume,
                remaining_frozen_volume=order_volume,
                consumed_volume=0,
                released_volume=0,
                status="ACTIVE",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        db.commit()


def command(event_id, price, volume):
    return SettlementCommand(
        order_id="O-CLOSE",
        market_event_id=event_id,
        market_stream_message_id=f"{event_id}-0",
        tick_event_time=NOW,
        tick_sequence_id=1,
        match_result=MatchResult(
            matched=True,
            fill_price=Decimal(price),
            fill_volume=volume,
            reason=None,
            engine_name="VN",
            engine_version="1.0",
        ),
    )


@pytest.mark.parametrize(
    ("direction", "close_price", "expected_pnl"),
    [
        ("SELL", "3522", Decimal("660.000000")),
        ("SELL", "3480", Decimal("-600.000000")),
        ("BUY", "3480", Decimal("600.000000")),
        ("BUY", "3522", Decimal("-660.000000")),
    ],
)
def test_close_settlement_updates_trade_account_and_position(
    direction,
    close_price,
    expected_pnl,
):
    session_factory = factory()
    seed_close(session_factory, close_direction=direction)
    with session_factory() as db:
        result = TradeSettlementService().settle(
            db,
            command("TICK-1", close_price, 3),
        )
        assert result.action == "SETTLED"

    with session_factory() as db:
        order = db.scalar(select(Order))
        account = db.scalar(select(Account))
        position = db.scalar(select(Position))
        detail = db.scalar(select(PositionDetail))
        allocation = db.scalar(select(PositionFreezeAllocation))
        trade = db.scalar(select(Trade))
        assert order.status == "PARTIALLY_FILLED"
        assert (order.traded_volume, order.remaining_volume) == (3, 1)
        assert order.frozen_position_volume == 1
        assert order.frozen_commission == Decimal("6.000000")
        assert trade.realized_pnl == expected_pnl
        assert trade.margin == Decimal("12600.000000")
        assert trade.commission == Decimal("18.000000")
        assert account.used_margin == Decimal("29400.000000")
        assert account.realized_pnl == expected_pnl
        assert position.total_volume == 7
        assert position.frozen_volume == 1
        assert position.available_volume == 6
        assert detail.remaining_volume == 7
        assert detail.frozen_volume == 1
        assert detail.remaining_margin == Decimal("29400.000000")
        assert allocation.remaining_frozen_volume == 1
        assert allocation.consumed_volume == 3
        assert len(db.scalars(select(OutboxEvent)).all()) == 2


def test_partial_close_then_cancel_releases_only_remaining_allocation():
    session_factory = factory()
    seed_close(session_factory)
    with session_factory() as db:
        TradeSettlementService().settle(
            db,
            command("TICK-1", "3522", 3),
        )
    with session_factory() as db:
        result = OrderCancellationService().cancel_order(
            db=db,
            order_id="O-CLOSE",
            request=OrderCancelRequest(account_id="A001"),
        )
        assert result.status == "PARTIALLY_CANCELLED"

    with session_factory() as db:
        order = db.scalar(select(Order))
        position = db.scalar(select(Position))
        detail = db.scalar(select(PositionDetail))
        allocation = db.scalar(select(PositionFreezeAllocation))
        assert (order.traded_volume, order.cancelled_volume) == (3, 1)
        assert order.frozen_position_volume == 0
        assert position.total_volume == 7
        assert position.frozen_volume == 0
        assert position.available_volume == 7
        assert detail.remaining_volume == 7
        assert detail.frozen_volume == 0
        assert allocation.consumed_volume == 3
        assert allocation.released_volume == 1
        assert allocation.status == "RELEASED"


def test_last_close_releases_all_margin_tail():
    session_factory = factory()
    seed_close(
        session_factory,
        order_volume=3,
        position_volume=3,
        open_margin=Decimal("100.000001"),
    )
    with session_factory() as db:
        TradeSettlementService().settle(
            db,
            command("TICK-LAST", "3500", 3),
        )
    with session_factory() as db:
        assert db.scalar(select(Trade)).margin == Decimal("100.000001")
        assert db.scalar(select(Account)).used_margin == Decimal("0.000000")
        position = db.scalar(select(Position))
        assert position.total_volume == 0
        assert position.used_margin == Decimal("0.000000")
        assert db.scalar(select(PositionDetail)).remaining_margin == Decimal(
            "0.000000"
        )


def test_multiple_details_are_closed_fifo_and_pnl_is_summed_per_detail():
    session_factory = factory()
    seed_close(session_factory)
    with session_factory() as db:
        old_detail = db.scalar(select(PositionDetail))
        old_allocation = db.scalar(select(PositionFreezeAllocation))
        db.delete(old_allocation)
        db.delete(old_detail)
        db.flush()
        db.add_all(
            [
                PositionDetail(
                    position_detail_id="PD-1",
                    position_id="P-1",
                    account_id="A001",
                    open_trade_id="T-OPEN-1",
                    order_book_id="AG2609",
                    exchange_id="SHFE",
                    symbol="AG2609",
                    direction="LONG",
                    open_trading_day=TRADING_DAY,
                    open_price=Decimal("3500"),
                    original_volume=2,
                    remaining_volume=2,
                    frozen_volume=2,
                    open_margin=Decimal("8400"),
                    remaining_margin=Decimal("8400"),
                    open_commission=Decimal("6"),
                    status="OPEN",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                PositionDetail(
                    position_detail_id="PD-2",
                    position_id="P-1",
                    account_id="A001",
                    open_trade_id="T-OPEN-2",
                    order_book_id="AG2609",
                    exchange_id="SHFE",
                    symbol="AG2609",
                    direction="LONG",
                    open_trading_day=TRADING_DAY,
                    open_price=Decimal("3510"),
                    original_volume=8,
                    remaining_volume=8,
                    frozen_volume=2,
                    open_margin=Decimal("33600"),
                    remaining_margin=Decimal("33600"),
                    open_commission=Decimal("24"),
                    status="OPEN",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                PositionFreezeAllocation(
                    allocation_id="PFA-1",
                    order_id="O-CLOSE",
                    position_id="P-1",
                    position_detail_id="PD-1",
                    account_id="A001",
                    exchange_id="SHFE",
                    symbol="AG2609",
                    offset_flag="CLOSE_TODAY",
                    original_frozen_volume=2,
                    remaining_frozen_volume=2,
                    consumed_volume=0,
                    released_volume=0,
                    status="ACTIVE",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                PositionFreezeAllocation(
                    allocation_id="PFA-2",
                    order_id="O-CLOSE",
                    position_id="P-1",
                    position_detail_id="PD-2",
                    account_id="A001",
                    exchange_id="SHFE",
                    symbol="AG2609",
                    offset_flag="CLOSE_TODAY",
                    original_frozen_volume=2,
                    remaining_frozen_volume=2,
                    consumed_volume=0,
                    released_volume=0,
                    status="ACTIVE",
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        db.commit()

    with session_factory() as db:
        TradeSettlementService().settle(
            db,
            command("TICK-FIFO", "3520", 3),
        )
    with session_factory() as db:
        details = db.scalars(
            select(PositionDetail).order_by(PositionDetail.id)
        ).all()
        trade = db.scalar(select(Trade))
        position = db.scalar(select(Position))
        assert trade.realized_pnl == Decimal("500.000000")
        assert trade.margin == Decimal("12600.000000")
        assert details[0].remaining_volume == 0
        assert details[1].remaining_volume == 7
        assert position.average_open_price == Decimal("3510.000000")
        assert position.position_cost == Decimal("245700.000000")


class FailingOutbox(OutboxRepository):
    @staticmethod
    def create_event(*args, **kwargs):
        raise RuntimeError("close outbox failed")


def test_close_outbox_failure_rolls_back_everything():
    session_factory = factory()
    seed_close(session_factory)
    with session_factory() as db:
        with pytest.raises(RuntimeError, match="close outbox failed"):
            TradeSettlementService(outbox_repository=FailingOutbox()).settle(
                db,
                command("TICK-FAIL", "3522", 3),
            )
    with session_factory() as db:
        order = db.scalar(select(Order))
        account = db.scalar(select(Account))
        position = db.scalar(select(Position))
        detail = db.scalar(select(PositionDetail))
        allocation = db.scalar(select(PositionFreezeAllocation))
        assert order.status == "ACCEPTED"
        assert order.remaining_volume == 4
        assert account.used_margin == Decimal("42000.000000")
        assert position.total_volume == 10
        assert detail.remaining_volume == 10
        assert allocation.remaining_frozen_volume == 4
        assert len(db.scalars(select(Trade)).all()) == 0
        assert len(db.scalars(select(OutboxEvent)).all()) == 0
