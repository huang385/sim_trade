from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.common.exceptions import DataAccessError
from app.core.database import Base
from app.matching.types import MatchResult
from app.models.account import Account
from app.models.instrument import Instrument
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.position_freeze_allocation import PositionFreezeAllocation
from app.models.trade import Trade
from app.models.trade_position_allocation import TradePositionAllocation
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.trade_position_allocation_repository import (
    TradePositionAllocationRepository,
)
from app.schemas.order_schema import OrderCancelRequest
from app.services.order_cancellation_service import OrderCancellationService
from app.services.account_access_scope import AccountAccessScope
from app.services.close_trade_settlement_handler import (
    CloseTradeSettlementHandler,
)
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
                user_id="U001",
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
                commission_type="BY_VOLUME",
                commission_parameter=Decimal("6"),
                commission_contract_multiplier=Decimal("10"),
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
                multiplier_snapshot=Decimal("10"),
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
                multiplier_snapshot=Decimal("10"),
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
                resolved_offset_flag="CLOSE_TODAY",
                commission_type="BY_VOLUME",
                commission_parameter=Decimal("6"),
                commission_contract_multiplier=Decimal("10"),
                original_frozen_volume=order_volume,
                remaining_frozen_volume=order_volume,
                consumed_volume=0,
                released_volume=0,
                original_frozen_commission=frozen_commission,
                remaining_frozen_commission=frozen_commission,
                consumed_commission=Decimal("0"),
                released_commission=Decimal("0"),
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


def test_close_uses_detail_multiplier_snapshot_after_instrument_changes():
    session_factory = factory()
    seed_close(session_factory)
    with session_factory() as db:
        instrument = db.scalar(select(Instrument))
        instrument.contract_multiplier = Decimal("99")
        db.commit()

    with session_factory() as db:
        result = TradeSettlementService().settle(
            db,
            command("TICK-SNAPSHOT", "3522", 3),
        )
        assert result.action == "SETTLED"

    with session_factory() as db:
        trade = db.scalar(select(Trade))
        assert trade.realized_pnl == Decimal("660.000000")
        assert trade.turnover == Decimal("105660.000000")


def configure_option_close(
    session_factory,
    *,
    close_direction: str,
) -> None:
    """把通用平仓夹具转换成带可靠标记市值的商品期权持仓。"""

    is_short = close_direction == "BUY"
    with session_factory() as db:
        order = db.scalar(select(Order))
        account = db.scalar(select(Account))
        position = db.scalar(select(Position))
        detail = db.scalar(select(PositionDetail))
        instrument = db.scalar(select(Instrument))

        underlying = Instrument(
            order_book_id="AG-UNDERLYING",
            symbol="AG-UNDERLYING",
            exchange_id="SHFE",
            instrument_type="FUTURES",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("1"),
            min_volume=1,
            max_volume=100,
            is_active=True,
        )
        db.add(underlying)
        db.flush()

        order.instrument_type = "FUTURES_OPTION"
        instrument.instrument_type = "FUTURES_OPTION"
        instrument.underlying_instrument_id = underlying.id
        instrument.option_type = "CALL"
        instrument.strike_price = Decimal("3500")
        instrument.expire_date = date(2026, 9, 30)
        order.direction = close_direction
        order.limit_price = Decimal("25")
        position.instrument_type = "FUTURES_OPTION"
        position.direction = "SHORT" if is_short else "LONG"
        position.average_open_price = Decimal("20")
        position.position_cost = Decimal("2000")
        position.multiplier_snapshot = Decimal("10")
        position.option_market_value = Decimal("2000")
        position.realtime_required_margin = (
            Decimal("8000") if is_short else Decimal("0")
        )
        detail.direction = position.direction
        detail.open_price = Decimal("20")
        detail.multiplier_snapshot = Decimal("10")
        detail.realtime_required_margin = (
            Decimal("8000") if is_short else Decimal("0")
        )

        if is_short:
            position.used_margin = Decimal("5000")
            detail.open_margin = Decimal("5000")
            detail.remaining_margin = Decimal("5000")
            account.cash_balance = Decimal("102000")
            account.used_margin = Decimal("5000")
            account.option_used_margin = Decimal("5000")
            account.option_realtime_required_margin = Decimal("8000")
            account.long_option_market_value = Decimal("0")
            account.short_option_market_value = Decimal("2000")
        else:
            position.used_margin = Decimal("0")
            detail.open_margin = Decimal("0")
            detail.remaining_margin = Decimal("0")
            account.cash_balance = Decimal("98000")
            account.used_margin = Decimal("0")
            account.option_used_margin = Decimal("0")
            account.option_realtime_required_margin = Decimal("0")
            account.long_option_market_value = Decimal("2000")
            account.short_option_market_value = Decimal("0")
        account.net_option_market_value = (
            Decimal("-2000") if is_short else Decimal("2000")
        )
        db.commit()


@pytest.mark.parametrize(
    ("close_direction", "market_field"),
    [
        ("SELL", "long_option_market_value"),
        ("BUY", "short_option_market_value"),
    ],
)
def test_option_partial_close_releases_mark_value_not_trade_turnover(
    close_direction,
    market_field,
):
    """平仓价与标记价不同时，只按旧持仓标记市值比例扣减。"""

    session_factory = factory()
    seed_close(
        session_factory,
        close_direction=close_direction,
        order_volume=4,
        position_volume=10,
        open_margin=(
            Decimal("5000")
            if close_direction == "BUY"
            else Decimal("0")
        ),
    )
    configure_option_close(
        session_factory,
        close_direction=close_direction,
    )

    with session_factory() as db:
        result = TradeSettlementService().settle(
            db,
            command("TICK-OPTION-CLOSE", "25", 4),
        )
        assert result.action == "SETTLED"

    with session_factory() as db:
        account = db.scalar(select(Account))
        position = db.scalar(select(Position))
        detail = db.scalar(select(PositionDetail))
        trade = db.scalar(select(Trade))

        # 原标记市值2000，平4/10只扣800；成交额1000仅用于权利金现金流。
        assert trade.turnover == Decimal("1000.000000")
        assert getattr(account, market_field) == Decimal("1200.000000")
        assert position.option_market_value == Decimal("1200.000000")
        assert position.total_volume == 6
        if close_direction == "BUY":
            assert account.option_realtime_required_margin == Decimal(
                "4800.000000"
            )
            assert position.realtime_required_margin == Decimal(
                "4800.000000"
            )
            assert detail.realtime_required_margin == Decimal("4800.000000")
        else:
            assert account.option_realtime_required_margin == Decimal(
                "0.000000"
            )


def test_option_full_close_clears_position_market_value_and_realtime_margin():
    session_factory = factory()
    seed_close(
        session_factory,
        close_direction="BUY",
        order_volume=10,
        position_volume=10,
        open_margin=Decimal("5000"),
    )
    configure_option_close(session_factory, close_direction="BUY")

    with session_factory() as db:
        result = TradeSettlementService().settle(
            db,
            command("TICK-OPTION-FULL-CLOSE", "25", 10),
        )
        assert result.action == "SETTLED"

    with session_factory() as db:
        account = db.scalar(select(Account))
        position = db.scalar(select(Position))
        assert account.short_option_market_value == Decimal("0.000000")
        assert account.option_realtime_required_margin == Decimal("0.000000")
        assert position.option_market_value == Decimal("0.000000")
        assert position.realtime_required_margin == Decimal("0.000000")
        assert position.total_volume == 0
        events = db.scalars(select(OutboxEvent)).all()
        assert {event.event_type for event in events} == {
            "TRADE_CREATED",
            "ORDER_FILLED",
            "POSITION_CLOSED",
            "ACCOUNT_FACT_UPDATED",
        }


def seed_cross_day_close(session_factory):
    """构造昨仓5手、今仓5手的普通 CLOSE 订单。"""

    seed_close(session_factory, order_volume=4)
    with session_factory() as db:
        order = db.scalar(select(Order))
        account = db.scalar(select(Account))
        position = db.scalar(select(Position))
        old_detail = db.scalar(select(PositionDetail))
        old_allocation = db.scalar(select(PositionFreezeAllocation))
        db.delete(old_allocation)
        db.delete(old_detail)
        db.flush()

        order.offset_flag = "CLOSE"
        order.total_volume = 10
        order.remaining_volume = 10
        order.frozen_position_volume = 10
        order.frozen_commission = Decimal("45")
        account.frozen_commission = Decimal("45")
        account.available_cash = Decimal("57925")
        position.today_volume = 5
        position.yesterday_volume = 5
        position.frozen_volume = 10
        position.available_volume = 0

        details = [
            PositionDetail(
                position_detail_id="PD-Y",
                position_id="P-1",
                account_id="A001",
                open_trade_id="T-OPEN-Y",
                order_book_id="AG2609",
                exchange_id="SHFE",
                symbol="AG2609",
                direction="LONG",
                open_trading_day=TRADING_DAY.replace(day=23),
                open_price=Decimal("3500"),
                original_volume=5,
                remaining_volume=5,
                frozen_volume=5,
                open_margin=Decimal("21000"),
                remaining_margin=Decimal("21000"),
                multiplier_snapshot=Decimal("10"),
                open_commission=Decimal("15"),
                status="OPEN",
                created_at=NOW,
                updated_at=NOW,
            ),
            PositionDetail(
                position_detail_id="PD-T",
                position_id="P-1",
                account_id="A001",
                open_trade_id="T-OPEN-T",
                order_book_id="AG2609",
                exchange_id="SHFE",
                symbol="AG2609",
                direction="LONG",
                open_trading_day=TRADING_DAY,
                open_price=Decimal("3510"),
                original_volume=5,
                remaining_volume=5,
                frozen_volume=5,
                open_margin=Decimal("21000"),
                remaining_margin=Decimal("21000"),
                multiplier_snapshot=Decimal("10"),
                open_commission=Decimal("15"),
                status="OPEN",
                created_at=NOW,
                updated_at=NOW,
            ),
        ]
        db.add_all(details)
        db.flush()
        db.add_all(
            [
                PositionFreezeAllocation(
                    allocation_id="PFA-Y",
                    order_id="O-CLOSE",
                    position_id="P-1",
                    position_detail_id="PD-Y",
                    account_id="A001",
                    exchange_id="SHFE",
                    symbol="AG2609",
                    offset_flag="CLOSE",
                    resolved_offset_flag="CLOSE_YESTERDAY",
                    commission_type="BY_VOLUME",
                    commission_parameter=Decimal("3"),
                    commission_contract_multiplier=Decimal("10"),
                    original_frozen_volume=5,
                    remaining_frozen_volume=5,
                    consumed_volume=0,
                    released_volume=0,
                    original_frozen_commission=Decimal("15"),
                    remaining_frozen_commission=Decimal("15"),
                    consumed_commission=Decimal("0"),
                    released_commission=Decimal("0"),
                    status="ACTIVE",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                PositionFreezeAllocation(
                    allocation_id="PFA-T",
                    order_id="O-CLOSE",
                    position_id="P-1",
                    position_detail_id="PD-T",
                    account_id="A001",
                    exchange_id="SHFE",
                    symbol="AG2609",
                    offset_flag="CLOSE",
                    resolved_offset_flag="CLOSE_TODAY",
                    commission_type="BY_VOLUME",
                    commission_parameter=Decimal("6"),
                    commission_contract_multiplier=Decimal("10"),
                    original_frozen_volume=5,
                    remaining_frozen_volume=5,
                    consumed_volume=0,
                    released_volume=0,
                    original_frozen_commission=Decimal("30"),
                    remaining_frozen_commission=Decimal("30"),
                    consumed_commission=Decimal("0"),
                    released_commission=Decimal("0"),
                    status="ACTIVE",
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        db.commit()


def configure_tail_fee(session_factory):
    """把两手平仓订单改成可稳定复现六位量化尾差的按金额费率。"""

    with session_factory() as db:
        order = db.scalar(select(Order))
        account = db.scalar(select(Account))
        allocation = db.scalar(select(PositionFreezeAllocation))
        frozen_commission = Decimal("0.070401")

        order.commission_type = "BY_AMOUNT"
        order.commission_parameter = Decimal("0.000001000015")
        order.commission_contract_multiplier = Decimal("10")
        order.frozen_commission = frozen_commission

        allocation.commission_type = "BY_AMOUNT"
        allocation.commission_parameter = Decimal("0.000001000015")
        allocation.commission_contract_multiplier = Decimal("10")
        allocation.original_frozen_commission = frozen_commission
        allocation.remaining_frozen_commission = frozen_commission

        account.frozen_commission = frozen_commission
        account.available_cash = Decimal("57970") - frozen_commission
        db.commit()


def split_tail_position_into_two_details(session_factory):
    """把同一费用桶的两手持仓拆成两条各一手明细。"""

    with session_factory() as db:
        old_detail = db.scalar(select(PositionDetail))
        old_allocation = db.scalar(select(PositionFreezeAllocation))
        db.delete(old_allocation)
        db.delete(old_detail)
        db.flush()

        details = []
        allocations = []
        commission_shares = (
            Decimal("0.035201"),
            Decimal("0.035200"),
        )
        for index, commission in enumerate(commission_shares, start=1):
            detail_id = f"PD-{index}"
            details.append(
                PositionDetail(
                    position_detail_id=detail_id,
                    position_id="P-1",
                    account_id="A001",
                    open_trade_id=f"T-OPEN-{index}",
                    order_book_id="AG2609",
                    exchange_id="SHFE",
                    symbol="AG2609",
                    direction="LONG",
                    open_trading_day=TRADING_DAY,
                    open_price=Decimal("3500"),
                    original_volume=1,
                    remaining_volume=1,
                    frozen_volume=1,
                    open_margin=Decimal("4200"),
                    remaining_margin=Decimal("4200"),
                    multiplier_snapshot=Decimal("10"),
                    open_commission=Decimal("15"),
                    status="OPEN",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            allocations.append(
                PositionFreezeAllocation(
                    allocation_id=f"PFA-{index}",
                    order_id="O-CLOSE",
                    position_id="P-1",
                    position_detail_id=detail_id,
                    account_id="A001",
                    exchange_id="SHFE",
                    symbol="AG2609",
                    offset_flag="CLOSE_TODAY",
                    resolved_offset_flag="CLOSE_TODAY",
                    commission_type="BY_AMOUNT",
                    commission_parameter=Decimal("0.000001000015"),
                    commission_contract_multiplier=Decimal("10"),
                    original_frozen_volume=1,
                    remaining_frozen_volume=1,
                    consumed_volume=0,
                    released_volume=0,
                    original_frozen_commission=commission,
                    remaining_frozen_commission=commission,
                    consumed_commission=Decimal("0"),
                    released_commission=Decimal("0"),
                    status="ACTIVE",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        db.add_all(details)
        db.flush()
        db.add_all(allocations)
        db.commit()


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
        assert allocation.remaining_frozen_commission == Decimal("6.000000")
        assert allocation.consumed_commission == Decimal("18.000000")
        trade_allocations = db.scalars(
            select(TradePositionAllocation)
        ).all()
        assert len(trade_allocations) == 1
        assert trade_allocations[0].position_detail_id == "PD-1"
        assert trade_allocations[0].close_volume == 3
        assert trade_allocations[0].commission == Decimal("18.000000")
        events = db.scalars(select(OutboxEvent)).all()
        assert len(events) == 4
        assert {event.event_type for event in events} == {
            "TRADE_CREATED",
            "ORDER_PARTIALLY_FILLED",
            "POSITION_UPDATED",
            "ACCOUNT_FACT_UPDATED",
        }


def test_partial_close_then_cancel_releases_only_remaining_allocation():
    session_factory = factory()
    seed_close(session_factory)
    with session_factory() as db:
        TradeSettlementService().settle(
            db,
            command("TICK-1", "3522", 3),
        )
    with session_factory() as db:
        result = OrderCancellationService(
            default_access_scope=AccountAccessScope.admin()
        ).cancel_order(
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
        assert allocation.consumed_commission == Decimal("18.000000")
        assert allocation.released_commission == Decimal("6.000000")
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
        events = db.scalars(select(OutboxEvent)).all()
        assert {event.event_type for event in events} == {
            "TRADE_CREATED",
            "ORDER_FILLED",
            "POSITION_CLOSED",
            "ACCOUNT_FACT_UPDATED",
        }


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
                    multiplier_snapshot=Decimal("10"),
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
                    multiplier_snapshot=Decimal("10"),
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
                    resolved_offset_flag="CLOSE_TODAY",
                    commission_type="BY_VOLUME",
                    commission_parameter=Decimal("6"),
                    commission_contract_multiplier=Decimal("10"),
                    original_frozen_volume=2,
                    remaining_frozen_volume=2,
                    consumed_volume=0,
                    released_volume=0,
                    original_frozen_commission=Decimal("12"),
                    remaining_frozen_commission=Decimal("12"),
                    consumed_commission=Decimal("0"),
                    released_commission=Decimal("0"),
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
                    resolved_offset_flag="CLOSE_TODAY",
                    commission_type="BY_VOLUME",
                    commission_parameter=Decimal("6"),
                    commission_contract_multiplier=Decimal("10"),
                    original_frozen_volume=2,
                    remaining_frozen_volume=2,
                    consumed_volume=0,
                    released_volume=0,
                    original_frozen_commission=Decimal("12"),
                    remaining_frozen_commission=Decimal("12"),
                    consumed_commission=Decimal("0"),
                    released_commission=Decimal("0"),
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
        allocations = db.scalars(
            select(TradePositionAllocation).order_by(
                TradePositionAllocation.id
            )
        ).all()
        assert [item.position_detail_id for item in allocations] == [
            "PD-1",
            "PD-2",
        ]
        assert [item.close_volume for item in allocations] == [2, 1]
        assert sum(item.close_volume for item in allocations) == (
            trade.trade_volume
        )
        assert sum(item.released_margin for item in allocations) == (
            trade.margin
        )
        assert sum(item.realized_pnl for item in allocations) == (
            trade.realized_pnl
        )
        assert sum(item.commission for item in allocations) == (
            trade.commission
        )


def test_close_by_amount_uses_fill_price_and_reconciles_frozen_difference():
    session_factory = factory()
    seed_close(session_factory)
    with session_factory() as db:
        order = db.scalar(select(Order))
        account = db.scalar(select(Account))
        allocation = db.scalar(select(PositionFreezeAllocation))
        order.commission_type = "BY_AMOUNT"
        order.commission_parameter = Decimal("0.0001")
        order.commission_contract_multiplier = Decimal("10")
        order.limit_price = Decimal("3500")
        order.frozen_commission = Decimal("14.000000")
        allocation.commission_type = "BY_AMOUNT"
        allocation.commission_parameter = Decimal("0.0001")
        allocation.commission_contract_multiplier = Decimal("10")
        allocation.original_frozen_commission = Decimal("14.000000")
        allocation.remaining_frozen_commission = Decimal("14.000000")
        account.frozen_commission = Decimal("14.000000")
        account.available_cash = Decimal("57956.000000")
        db.commit()

    with session_factory() as db:
        TradeSettlementService().settle(
            db,
            command("TICK-AMOUNT", "3522", 3),
        )

    with session_factory() as db:
        order = db.scalar(select(Order))
        account = db.scalar(select(Account))
        trade = db.scalar(select(Trade))
        allocation = db.scalar(select(PositionFreezeAllocation))
        detail = db.scalar(select(TradePositionAllocation))
        # 预计释放：3500*3*10*0.0001=10.5；实际：3522*=10.566。
        assert trade.commission == Decimal("10.566000")
        assert detail.commission == Decimal("10.566000")
        assert order.frozen_commission == Decimal("3.500000")
        assert allocation.remaining_frozen_commission == Decimal("3.500000")
        assert allocation.consumed_commission == Decimal("10.500000")
        assert account.frozen_commission == Decimal("3.500000")
        assert account.used_commission == Decimal("40.566000")
        assert account.available_cash == Decimal("71215.934000")

    with session_factory() as db:
        OrderCancellationService(
            default_access_scope=AccountAccessScope.admin()
        ).cancel_order(
            db=db,
            order_id="O-CLOSE",
            request=OrderCancelRequest(account_id="A001"),
        )
    with session_factory() as db:
        order = db.scalar(select(Order))
        account = db.scalar(select(Account))
        allocation = db.scalar(select(PositionFreezeAllocation))
        assert order.status == "PARTIALLY_CANCELLED"
        assert account.frozen_commission == Decimal("0.000000")
        assert account.available_cash == Decimal("71219.434000")
        assert allocation.consumed_commission == Decimal("10.500000")
        assert allocation.released_commission == Decimal("3.500000")


def test_by_amount_partial_fills_consume_last_frozen_commission_tail():
    """第二次部分成交必须消费剩余资源，不得按一手重新量化后拒绝。"""

    session_factory = factory()
    seed_close(
        session_factory,
        order_volume=2,
        position_volume=2,
        open_margin=Decimal("8400"),
    )
    configure_tail_fee(session_factory)

    service = TradeSettlementService()
    with session_factory() as db:
        first = service.settle(
            db,
            command("TICK-TAIL-1", "3520", 1),
        )
        assert first.action == "SETTLED"
    with session_factory() as db:
        second = service.settle(
            db,
            command("TICK-TAIL-2", "3520", 1),
        )
        assert second.action == "SETTLED"

    with session_factory() as db:
        order = db.scalar(select(Order))
        account = db.scalar(select(Account))
        allocation = db.scalar(select(PositionFreezeAllocation))
        trades = db.scalars(select(Trade).order_by(Trade.id)).all()

        assert order.status == "FILLED"
        assert order.frozen_commission == Decimal("0.000000")
        assert allocation.remaining_frozen_commission == Decimal("0.000000")
        assert allocation.consumed_commission == Decimal("0.070401")
        assert (
            allocation.original_frozen_commission
            == allocation.remaining_frozen_commission
            + allocation.consumed_commission
            + allocation.released_commission
        )
        # 每笔Trade按本次成交量独立计算实际手续费；预计冻结资源则严格
        # 守恒，第二笔只释放首笔比例分配后留下的0.035200。
        assert [item.commission for item in trades] == [
            Decimal("0.035201"),
            Decimal("0.035201"),
        ]
        assert account.frozen_commission == Decimal("0.000000")
        assert account.used_commission == Decimal("30.070402")
        assert account.available_cash == Decimal("66769.929598")


def test_fee_bucket_total_is_same_for_one_or_two_position_details():
    """同桶两手拆成一条或两条持仓时，冻结、成交和资金结果必须一致。"""

    snapshots = []
    for split_details in (False, True):
        session_factory = factory()
        seed_close(
            session_factory,
            order_volume=2,
            position_volume=2,
            open_margin=Decimal("8400"),
        )
        configure_tail_fee(session_factory)
        if split_details:
            split_tail_position_into_two_details(session_factory)

        with session_factory() as db:
            order = db.scalar(select(Order))
            allocations = db.scalars(
                select(PositionFreezeAllocation).order_by(
                    PositionFreezeAllocation.id
                )
            ).all()
            assert sum(
                item.original_frozen_commission for item in allocations
            ) == order.frozen_commission == Decimal("0.070401")

        with session_factory() as db:
            TradeSettlementService().settle(
                db,
                command(
                    f"TICK-BUCKET-{int(split_details)}",
                    "3520",
                    2,
                ),
            )

        with session_factory() as db:
            trade = db.scalar(select(Trade))
            account = db.scalar(select(Account))
            trade_allocations = db.scalars(
                select(TradePositionAllocation).order_by(
                    TradePositionAllocation.id
                )
            ).all()
            assert sum(
                item.commission for item in trade_allocations
            ) == trade.commission == Decimal("0.070401")
            if split_details:
                assert [item.commission for item in trade_allocations] == [
                    Decimal("0.035201"),
                    Decimal("0.035200"),
                ]
            snapshots.append(
                (
                    trade.commission,
                    account.cash_balance,
                    account.available_cash,
                    account.used_commission,
                )
            )

    assert snapshots[0] == snapshots[1]


def test_plain_close_partial_fills_charge_yesterday_then_cross_into_today():
    session_factory = factory()
    seed_cross_day_close(session_factory)

    service = TradeSettlementService()
    with session_factory() as db:
        service.settle(db, command("TICK-CROSS-1", "3520", 3))
    with session_factory() as db:
        service.settle(db, command("TICK-CROSS-2", "3520", 4))
    with session_factory() as db:
        service.settle(db, command("TICK-CROSS-3", "3520", 3))

    with session_factory() as db:
        trades = db.scalars(select(Trade).order_by(Trade.id)).all()
        details = db.scalars(
            select(TradePositionAllocation).order_by(
                TradePositionAllocation.id
            )
        ).all()
        allocations = db.scalars(
            select(PositionFreezeAllocation).order_by(
                PositionFreezeAllocation.id
            )
        ).all()
        assert [item.commission for item in trades] == [
            Decimal("9.000000"),
            Decimal("18.000000"),
            Decimal("18.000000"),
        ]
        assert [
            (item.resolved_offset_flag, item.close_volume, item.commission)
            for item in details
        ] == [
            ("CLOSE_YESTERDAY", 3, Decimal("9.000000")),
            ("CLOSE_YESTERDAY", 2, Decimal("6.000000")),
            ("CLOSE_TODAY", 2, Decimal("12.000000")),
            ("CLOSE_TODAY", 3, Decimal("18.000000")),
        ]
        assert sum(item.commission for item in trades) == Decimal(
            "45.000000"
        )
        assert all(
            item.original_frozen_commission
            == item.remaining_frozen_commission
            + item.consumed_commission
            + item.released_commission
            for item in allocations
        )


def test_plain_close_partial_fill_then_cancel_releases_each_remaining_fee():
    session_factory = factory()
    seed_cross_day_close(session_factory)
    with session_factory() as db:
        TradeSettlementService().settle(
            db,
            command("TICK-CROSS-CANCEL", "3520", 3),
        )
    with session_factory() as db:
        OrderCancellationService(
            default_access_scope=AccountAccessScope.admin()
        ).cancel_order(
            db=db,
            order_id="O-CLOSE",
            request=OrderCancelRequest(account_id="A001"),
        )

    with session_factory() as db:
        order = db.scalar(select(Order))
        account = db.scalar(select(Account))
        allocations = db.scalars(
            select(PositionFreezeAllocation).order_by(
                PositionFreezeAllocation.id
            )
        ).all()
        assert order.status == "PARTIALLY_CANCELLED"
        assert order.frozen_commission == Decimal("0.000000")
        assert account.frozen_commission == Decimal("0.000000")
        assert [
            (
                item.consumed_commission,
                item.released_commission,
                item.remaining_frozen_commission,
            )
            for item in allocations
        ] == [
            (
                Decimal("9.000000"),
                Decimal("6.000000"),
                Decimal("0.000000"),
            ),
            (
                Decimal("0.000000"),
                Decimal("30.000000"),
                Decimal("0.000000"),
            ),
        ]


@pytest.mark.parametrize(
    "corrupt",
    [
        "ALLOCATION_GREATER",
        "ALLOCATION_SMALLER",
        "ORDER_FROZEN_MISMATCH",
        "MISSING_ALLOCATION",
        "MISSING_DETAIL",
    ],
)
def test_close_consistency_error_rolls_back_without_trade_or_outbox(corrupt):
    session_factory = factory()
    seed_close(session_factory)
    with session_factory() as db:
        order = db.scalar(select(Order))
        position = db.scalar(select(Position))
        detail = db.scalar(select(PositionDetail))
        allocation = db.scalar(select(PositionFreezeAllocation))
        if corrupt == "ALLOCATION_GREATER":
            allocation.original_frozen_volume = 5
            allocation.remaining_frozen_volume = 5
            detail.frozen_volume = 5
            position.frozen_volume = 5
            position.available_volume = 5
        elif corrupt == "ALLOCATION_SMALLER":
            allocation.original_frozen_volume = 3
            allocation.remaining_frozen_volume = 3
            detail.frozen_volume = 3
            position.frozen_volume = 3
            position.available_volume = 7
        elif corrupt == "ORDER_FROZEN_MISMATCH":
            order.frozen_position_volume = 3
        elif corrupt == "MISSING_ALLOCATION":
            db.delete(allocation)
        elif corrupt == "MISSING_DETAIL":
            db.delete(detail)
        db.commit()

    with session_factory() as db:
        with pytest.raises(DataAccessError) as exc_info:
            TradeSettlementService().settle(
                db,
                command(f"TICK-{corrupt}", "3522", 2),
            )
        assert exc_info.value.error_code in {
            "CLOSE_ALLOCATION_INCONSISTENT",
            "CLOSE_POSITION_INCONSISTENT",
        }

    with session_factory() as db:
        assert db.scalars(select(Trade)).all() == []
        assert db.scalars(select(TradePositionAllocation)).all() == []
        assert db.scalars(select(OutboxEvent)).all() == []
        order = db.scalar(select(Order))
        assert order.status == "ACCEPTED"
        assert order.traded_volume == 0


class FailingTradePositionAllocationRepository(
    TradePositionAllocationRepository
):
    @staticmethod
    def add(db, item):
        raise RuntimeError("trade position allocation failed")


def test_trade_position_allocation_failure_rolls_back_entire_close():
    session_factory = factory()
    seed_close(session_factory)
    close_handler = CloseTradeSettlementHandler(
        position_repository=TradeSettlementService().position_repository,
        allocation_repository=(
            TradeSettlementService().allocation_repository
        ),
        trade_repository=TradeSettlementService().trade_repository,
        trade_position_allocation_repository=(
            FailingTradePositionAllocationRepository()
        ),
    )
    with session_factory() as db:
        with pytest.raises(
            RuntimeError,
            match="trade position allocation failed",
        ):
            TradeSettlementService(close_handler=close_handler).settle(
                db,
                command("TICK-DETAIL-FAIL", "3522", 3),
            )

    with session_factory() as db:
        assert db.scalars(select(Trade)).all() == []
        assert db.scalars(select(TradePositionAllocation)).all() == []
        assert db.scalar(select(Order)).status == "ACCEPTED"
        assert db.scalar(select(Account)).used_margin == Decimal(
            "42000.000000"
        )


def test_duplicate_close_settlement_does_not_duplicate_trade_details():
    session_factory = factory()
    seed_close(session_factory)
    service = TradeSettlementService()
    with session_factory() as db:
        first = service.settle(
            db,
            command("TICK-IDEMPOTENT", "3522", 3),
        )
    with session_factory() as db:
        second = service.settle(
            db,
            command("TICK-IDEMPOTENT", "3522", 3),
        )

    assert first.action == "SETTLED"
    assert second.action == "IDEMPOTENT"
    with session_factory() as db:
        assert len(db.scalars(select(Trade)).all()) == 1
        assert len(
            db.scalars(select(TradePositionAllocation)).all()
        ) == 1


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
