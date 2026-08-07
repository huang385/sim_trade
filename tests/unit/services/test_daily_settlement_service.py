from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.daily_settlement import (
    DailySettlementBatch,
)
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType
from app.services.daily_settlement_service import (
    DailySettlementError,
    DailySettlementService,
    SettlementInstrument,
)


TRADING_DAY = date(2026, 8, 6)
NEXT_DAY = date(2026, 8, 7)
NOW = datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)


@pytest.fixture
def sqlite_session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield engine, factory
    finally:
        engine.dispose()


def _instrument(
    *,
    instrument_id: int = 1,
    order_book_id: str = "FU2608",
    instrument_type: str = "FUTURES",
) -> SettlementInstrument:
    return SettlementInstrument(
        id=instrument_id,
        order_book_id=order_book_id,
        exchange_id="TEST",
        symbol=order_book_id,
        product_id="FU",
        instrument_type=instrument_type,
        multiplier=Decimal("10"),
        expire_date=None,
        last_trading_date=None,
        underlying_instrument_id=None,
        option_type=None,
        strike_price=None,
    )


def _tick_mapping(
    *,
    event_time: datetime = NOW,
    trading_day: date = TRADING_DAY,
    price: Decimal | None = Decimal("123.456789"),
    exchange_id: str = "TEST",
    symbol: str = "FU2608",
    order_book_id: str = "FU2608",
) -> dict[str, str]:
    tick = MarketTick(
        source_event_id="TICK-1",
        ingest_type=MarketTickIngestType.LIVE_CALLBACK,
        order_book_id=order_book_id,
        exchange_id=exchange_id,
        symbol=symbol,
        trading_day=trading_day,
        event_time=event_time,
        sequence_id=1,
        last_price=price,
        cumulative_volume=1,
        bid_volume_1=1,
        ask_volume_1=1,
    )
    from app.infrastructure.market_data.market_tick_store import MarketTickStore

    return MarketTickStore.tick_to_mapping(tick)


def test_validate_tick_uses_only_last_price_and_exact_decimal():
    service = DailySettlementService(
        time_provider=lambda: NOW,
        tick_max_age_seconds=3600,
        redis_recovery_enabled=False,
    )

    frozen = service._validate_tick(
        values=_tick_mapping(price=Decimal("123.4567894")),
        instrument=_instrument(),
        trading_day=TRADING_DAY,
    )

    assert frozen.price == Decimal("123.456789")
    assert isinstance(frozen.price, Decimal)


@pytest.mark.parametrize(
    ("values", "error_code"),
    [
        ({}, "SETTLEMENT_TICK_MISSING"),
        (_tick_mapping(price=None), "SETTLEMENT_PRICE_INVALID"),
        (_tick_mapping(price=Decimal("0")), "SETTLEMENT_PRICE_INVALID"),
        (
            _tick_mapping(exchange_id="OTHER"),
            "SETTLEMENT_TICK_CONTRACT_MISMATCH",
        ),
        (
            _tick_mapping(trading_day=date(2026, 8, 5)),
            "SETTLEMENT_TICK_TRADING_DAY_MISMATCH",
        ),
        (
            _tick_mapping(event_time=NOW - timedelta(seconds=3601)),
            "SETTLEMENT_TICK_STALE",
        ),
        (
            _tick_mapping(event_time=NOW + timedelta(seconds=61)),
            "SETTLEMENT_TICK_STALE",
        ),
    ],
)
def test_validate_tick_rejects_invalid_facts(values, error_code):
    service = DailySettlementService(
        time_provider=lambda: NOW,
        tick_max_age_seconds=3600,
        redis_recovery_enabled=False,
    )

    with pytest.raises(DailySettlementError) as raised:
        service._validate_tick(
            values=values,
            instrument=_instrument(),
            trading_day=TRADING_DAY,
        )

    assert raised.value.error_code == error_code


def test_allocate_margin_is_exact_and_assigns_rounding_remainder():
    details = [
        SimpleNamespace(
            remaining_volume=1,
            remaining_margin=Decimal("0"),
            realtime_required_margin=Decimal("0"),
        ),
        SimpleNamespace(
            remaining_volume=2,
            remaining_margin=Decimal("0"),
            realtime_required_margin=Decimal("0"),
        ),
    ]

    DailySettlementService._allocate_margin(details, Decimal("100.000000"))

    assert [item.remaining_margin for item in details] == [
        Decimal("33.333333"),
        Decimal("66.666667"),
    ]
    assert sum(item.remaining_margin for item in details) == Decimal("100.000000")


def _position(
    *,
    position_id: str,
    order_book_id: str,
    instrument_type: str,
    direction: str,
    total_volume: int,
    price: Decimal,
    used_margin: Decimal,
) -> Position:
    return Position(
        position_id=position_id,
        account_id="A-SETTLE",
        order_book_id=order_book_id,
        exchange_id="TEST",
        symbol=order_book_id,
        instrument_type=instrument_type,
        direction=direction,
        total_volume=total_volume,
        today_volume=total_volume,
        yesterday_volume=0,
        frozen_volume=0,
        available_volume=total_volume,
        average_open_price=price,
        position_cost=price * Decimal(total_volume) * Decimal("10"),
        used_margin=used_margin,
        initial_occupied_margin=used_margin,
        realtime_required_margin=used_margin if direction == "SHORT" else Decimal("0"),
        option_market_value=Decimal("0"),
        multiplier_snapshot=Decimal("10"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        daily_close_pnl=Decimal("0"),
        trading_day=TRADING_DAY,
    )


def _detail(
    *,
    detail_id: str,
    position_id: str,
    order_book_id: str,
    open_trade_id: str,
    open_price: Decimal,
    base_price: Decimal,
    volume: int,
    margin: Decimal,
    instrument_type: str,
    direction: str,
) -> PositionDetail:
    return PositionDetail(
        position_detail_id=detail_id,
        position_id=position_id,
        account_id="A-SETTLE",
        open_trade_id=open_trade_id,
        order_book_id=order_book_id,
        exchange_id="TEST",
        symbol=order_book_id,
        instrument_type=instrument_type,
        direction=direction,
        open_trading_day=TRADING_DAY,
        open_price=open_price,
        pnl_base_price=base_price,
        original_volume=volume,
        remaining_volume=volume,
        frozen_volume=0,
        open_margin=margin,
        remaining_margin=margin,
        initial_occupied_margin=margin,
        realtime_required_margin=margin if direction == "SHORT" else Decimal("0"),
        multiplier_snapshot=Decimal("10"),
        open_commission=Decimal("0"),
        status="OPEN",
    )


def test_redis_recovery_failure_keeps_completed_database_batch(
    sqlite_session_factory,
    monkeypatch,
):
    engine, factory = sqlite_session_factory
    batch = DailySettlementBatch(
        batch_id="B-CACHE-FAIL",
        trading_day=TRADING_DAY,
        status="COMPLETED",
        current_stage="COMPLETED",
        started_at=NOW,
        completed_at=NOW,
        cache_status="PENDING",
        created_at=NOW,
        updated_at=NOW,
    )
    with factory() as db:
        db.add(batch)
        db.commit()
        db.expunge(batch)

    def fail_redis(_client):
        raise RuntimeError("INJECTED_REDIS_FAILURE")

    monkeypatch.setattr(
        "app.services.daily_settlement_service.ActiveOrderIndex",
        fail_redis,
    )
    service = DailySettlementService(
        session_factory=factory,
        database_engine=engine,
        time_provider=lambda: NOW,
        redis_recovery_enabled=True,
    )

    status, message = service._recover_redis(batch)

    with factory() as db:
        persisted = db.scalar(select(DailySettlementBatch))
        assert status == "FAILED"
        assert message == "INJECTED_REDIS_FAILURE"
        assert persisted.status == "COMPLETED"
        assert persisted.cache_status == "FAILED"
