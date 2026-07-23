from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.account import Account
from app.models.instrument import Instrument
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.trade import Trade
from app.schemas.matching_schema import MatchResult
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.position_repository import PositionRepository
from app.repositories.trade_repository import TradeRepository
from app.services.trade_settlement_service import TradeSettlementService


def make_session_factory():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def seed(session_factory, *, direction="BUY"):
    now = datetime(2026, 7, 23, 1, tzinfo=timezone.utc)
    with session_factory() as db:
        db.add(
            Instrument(
                order_book_id="AG2609",
                symbol="AG2609",
                exchange_id="SHFE",
                contract_multiplier=Decimal("15"),
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
                initial_cash=Decimal("200000"),
                cash_balance=Decimal("200000"),
                available_cash=Decimal("68585"),
                frozen_cash=Decimal("0"),
                equity=Decimal("200000"),
                used_margin=Decimal("0"),
                frozen_margin=Decimal("131400"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                daily_pnl=Decimal("0"),
                used_commission=Decimal("0"),
                frozen_commission=Decimal("15"),
                risk_ratio=Decimal("0"),
                status="NORMAL",
                trading_day=date(2026, 7, 23),
            )
        )
        db.add(
            Order(
                order_id="O-1",
                client_order_id="C-1",
                account_id="A001",
                order_book_id="AG2609",
                symbol="AG2609",
                exchange_id="SHFE",
                trading_day=date(2026, 7, 23),
                direction=direction,
                offset_flag="OPEN",
                order_type="LIMIT",
                limit_price=Decimal("14600"),
                total_volume=5,
                traded_volume=0,
                remaining_volume=5,
                cancelled_volume=0,
                average_price=None,
                status="ACCEPTED",
                submit_status="ACCEPTED",
                frozen_margin=Decimal("131400"),
                frozen_commission=Decimal("15"),
                frozen_position_volume=0,
                created_at=now,
                accepted_at=now,
                updated_at=now,
            )
        )
        db.commit()


def match(event_id, price, volume):
    return MatchResult(
        matched=True,
        order_id="O-1",
        market_event_id=event_id,
        market_stream_message_id=f"{event_id}-0",
        fill_price=Decimal(price),
        fill_volume=volume,
        tick_event_time=datetime(2026, 7, 23, 2, tzinfo=timezone.utc),
        tick_sequence_id=1,
    )


def test_two_ticks_partial_then_full_preserve_all_balances():
    factory = make_session_factory()
    seed(factory)
    service = TradeSettlementService()
    with factory() as db:
        assert service.settle(db, match("TICK-1001", "14599", 2)).action == "SETTLED"
    with factory() as db:
        assert service.settle(db, match("TICK-1002", "14600", 3)).action == "SETTLED"

    with factory() as db:
        order = db.scalar(select(Order))
        account = db.scalar(select(Account))
        position = db.scalar(select(Position))
        trades = db.scalars(select(Trade).order_by(Trade.id)).all()
        details = db.scalars(select(PositionDetail)).all()
        outbox = db.scalars(select(OutboxEvent)).all()
        assert order.status == "FILLED"
        assert (order.traded_volume, order.remaining_volume, order.cancelled_volume) == (5, 0, 0)
        assert order.total_volume == order.traded_volume + order.remaining_volume + order.cancelled_volume
        assert order.average_price == Decimal("14599.600000")
        assert order.frozen_margin == Decimal("0.000000")
        assert order.frozen_commission == Decimal("0.000000")
        assert account.available_cash == Decimal("68585.000000")
        assert account.frozen_margin == Decimal("0.000000")
        assert account.used_margin == Decimal("131400.000000")
        assert account.frozen_commission == Decimal("0.000000")
        assert account.used_commission == Decimal("15.000000")
        assert account.cash_balance == Decimal("199985.000000")
        assert account.equity == Decimal("199985.000000")
        assert position.direction == "LONG"
        assert position.total_volume == position.today_volume == position.available_volume == 5
        assert position.average_open_price == Decimal("14599.600000")
        assert position.position_cost == Decimal("1094970.000000")
        assert position.used_margin == Decimal("131400.000000")
        assert [trade.margin for trade in trades] == [Decimal("52560.000000"), Decimal("78840.000000")]
        assert [trade.commission for trade in trades] == [Decimal("6.000000"), Decimal("9.000000")]
        assert len(details) == 2
        assert len(outbox) == 4
        assert {item.event_type for item in outbox} == {
            "TRADE_CREATED",
            "ORDER_PARTIALLY_FILLED",
            "ORDER_FILLED",
        }


def test_duplicate_market_event_is_idempotent_and_does_not_charge_again():
    factory = make_session_factory()
    seed(factory)
    service = TradeSettlementService()
    with factory() as db:
        first = service.settle(db, match("TICK-1", "14599", 2))
    with factory() as db:
        second = service.settle(db, match("TICK-1", "14599", 2))
    assert first.action == "SETTLED"
    assert second.action == "IDEMPOTENT"
    assert second.trade_id == first.trade_id
    with factory() as db:
        assert len(db.scalars(select(Trade)).all()) == 1
        assert len(db.scalars(select(PositionDetail)).all()) == 1
        assert db.scalar(select(Account)).used_commission == Decimal("6.000000")


def test_sell_open_creates_short_position():
    factory = make_session_factory()
    seed(factory, direction="SELL")
    with factory() as db:
        TradeSettlementService().settle(db, match("TICK-S", "14601", 1))
    with factory() as db:
        position = db.scalar(select(Position))
        assert position.direction == "SHORT"
        assert position.total_volume == 1


def test_filled_order_is_skipped_even_if_redis_candidate_remains():
    factory = make_session_factory()
    seed(factory)
    service = TradeSettlementService()
    with factory() as db:
        service.settle(db, match("TICK-1", "14599", 5))
    with factory() as db:
        result = service.settle(db, match("TICK-2", "14599", 1))
    assert result.action == "ORDER_INACTIVE"
    with factory() as db:
        assert len(db.scalars(select(Trade)).all()) == 1


class FailingTradeRepository(TradeRepository):
    @staticmethod
    def add(db, trade):
        raise RuntimeError("trade insert failed")


class FailingPositionRepository(PositionRepository):
    @staticmethod
    def add_detail(db, detail):
        raise RuntimeError("position detail insert failed")


class FailingSettlementOutboxRepository(OutboxRepository):
    @staticmethod
    def create_event(*args, **kwargs):
        raise RuntimeError("outbox insert failed")


def _assert_settlement_fully_rolled_back(factory):
    with factory() as db:
        order = db.scalar(select(Order))
        account = db.scalar(select(Account))
        assert order.status == "ACCEPTED"
        assert order.traded_volume == 0
        assert order.remaining_volume == 5
        assert order.frozen_margin == Decimal("131400.000000")
        assert account.frozen_margin == Decimal("131400.000000")
        assert account.used_margin == Decimal("0.000000")
        assert account.used_commission == Decimal("0.000000")
        assert len(db.scalars(select(Trade)).all()) == 0
        assert len(db.scalars(select(Position)).all()) == 0
        assert len(db.scalars(select(PositionDetail)).all()) == 0
        assert len(db.scalars(select(OutboxEvent)).all()) == 0


def test_trade_insert_failure_rolls_back_every_change():
    factory = make_session_factory()
    seed(factory)
    with factory() as db:
        with pytest.raises(RuntimeError, match="trade insert failed"):
            TradeSettlementService(
                trade_repository=FailingTradeRepository()
            ).settle(db, match("TICK-F", "14599", 2))
    _assert_settlement_fully_rolled_back(factory)


def test_position_detail_failure_rolls_back_every_change():
    factory = make_session_factory()
    seed(factory)
    with factory() as db:
        with pytest.raises(RuntimeError, match="position detail insert failed"):
            TradeSettlementService(
                position_repository=FailingPositionRepository()
            ).settle(db, match("TICK-F", "14599", 2))
    _assert_settlement_fully_rolled_back(factory)


def test_outbox_failure_rolls_back_every_change():
    factory = make_session_factory()
    seed(factory)
    with factory() as db:
        with pytest.raises(RuntimeError, match="outbox insert failed"):
            TradeSettlementService(
                outbox_repository=FailingSettlementOutboxRepository()
            ).settle(db, match("TICK-F", "14599", 2))
    _assert_settlement_fully_rolled_back(factory)
