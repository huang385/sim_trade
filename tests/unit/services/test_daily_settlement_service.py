from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.common.exceptions import DataAccessError
from app.core.database import Base
from app.models.daily_settlement import (
    DailySettlementBatch,
    InstrumentSettlementPrice,
)
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.infrastructure.market_data.settlement_last_tick_provider import (
    SettlementLastTick,
    SettlementLastTickPair,
)
from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType
from app.services.daily_settlement_service import (
    DailySettlementError,
    DailySettlementService,
    SettlementInstrument,
)


TRADING_DAY = date(2026, 8, 6)
NEXT_DAY = date(2026, 8, 7)
NOW = datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        ("BUY", Decimal("-1005.000000")),
        ("SELL", Decimal("995.000000")),
    ],
)
def test_cash_security_trade_cash_effect_uses_turnover_and_fee_once(
    direction, expected
):
    trade = SimpleNamespace(
        instrument_type="STOCK",
        direction=direction,
        turnover=Decimal("1000"),
        commission=Decimal("5"),
    )

    assert DailySettlementService._trade_cash_effect(trade) == expected


@pytest.mark.parametrize(
    ("ending_volume", "expected_holding_pnl"),
    [
        (3, Decimal("12.500000")),
        (0, Decimal("0")),
    ],
)
def test_futures_replay_keeps_close_pnl_after_position_is_fully_closed(
    ending_volume, expected_holding_pnl
):
    projection = SimpleNamespace(
        ending_volume=ending_volume,
        holding_pnl=Decimal("12.5"),
        close_pnl=Decimal("-80"),
    )

    holding_pnl, close_pnl = DailySettlementService._futures_replay_pnl_components(
        projection
    )

    assert holding_pnl == expected_holding_pnl
    assert close_pnl == Decimal("-80.000000")


def test_cash_security_corporate_action_adjustment_stream_is_only_validated_before_projection():
    position = SimpleNamespace(
        total_volume=100,
        frozen_volume=0,
        settlement_locked_volume=0,
    )
    adjustment = SimpleNamespace(
        effective_trading_day=TRADING_DAY,
        action_id="CA-MATURITY",
        action_version=3,
        component_id="COMPONENT",
        id=1,
        business_version="3",
        adjustment_type="BOND_MATURITY_RETIRED",
    )

    # The mutable Position is not a replay input.  A maturity adjustment is
    # consumed later by the cash-security projection, which will set it to
    # zero; validation here must not reject the stale aggregate first.
    DailySettlementService._consume_cash_security_adjustments(position, (adjustment,))


def test_cash_security_manual_review_fact_fails_closed_before_trade_replay():
    blocker = SimpleNamespace(
        position_id="P-SPLIT", action_id="CA-SPLIT", entry_type="STOCK_SPLIT",
        action_status="MANUAL_REVIEW_REQUIRED",
    )

    with pytest.raises(DataAccessError) as raised:
        DailySettlementService._assert_cash_security_replay_safe((blocker,))

    assert raised.value.error_code == "CASH_SECURITY_REPLAY_MANUAL_REVIEW_REQUIRED"


def test_cash_security_missing_quantity_fact_fails_closed_before_trade_replay():
    blocker = SimpleNamespace(
        position_id="P-MATURITY", action_id="CA-MATURITY",
        entry_type="BOND_PRINCIPAL_RECEIVABLE", action_status="COMPLETED",
    )

    with pytest.raises(DataAccessError) as raised:
        DailySettlementService._assert_cash_security_replay_safe((blocker,))

    assert raised.value.error_code == "CASH_SECURITY_REPLAY_FACT_INCOMPLETE"


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


def test_freeze_prices_persists_both_last_ticks_and_reuses_frozen_facts(
    sqlite_session_factory,
):
    engine, factory = sqlite_session_factory

    class Provider:
        calls = 0

        def fetch_many(self, codes, trading_day):
            self.calls += 1
            assert list(codes) == ["FU2608"]
            assert trading_day == TRADING_DAY
            return {
                "FU2608": SettlementLastTickPair(
                    current=SettlementLastTick(
                        order_book_id="FU2608",
                        trading_day=TRADING_DAY,
                        event_time=NOW,
                        last_price=Decimal("123"),
                        source_event_id="CURRENT",
                    ),
                    previous=SettlementLastTick(
                        order_book_id="FU2608",
                        trading_day=date(2026, 8, 5),
                        event_time=NOW - timedelta(days=1),
                        last_price=Decimal("120"),
                        source_event_id="PREVIOUS",
                    ),
                )
            }

    provider = Provider()
    service = DailySettlementService(
        session_factory=factory,
        database_engine=engine,
        settlement_price_provider=provider,
        time_provider=lambda: NOW,
        redis_recovery_enabled=False,
    )
    instrument = _instrument()
    kwargs = {
        "batch": SimpleNamespace(batch_id="B-LAST-TICK"),
        "trading_day": TRADING_DAY,
        "instruments": {instrument.order_book_id: instrument},
        "instruments_by_id": {instrument.id: instrument},
    }

    first = service._freeze_prices(**kwargs)
    second = service._freeze_prices(**kwargs)

    assert provider.calls == 1
    assert first[("TEST", "FU2608")].price == Decimal("123")
    assert first[("TEST", "FU2608")].previous_price == Decimal("120")
    assert second[("TEST", "FU2608")].price == Decimal("123.000000")
    assert second[("TEST", "FU2608")].previous_price == Decimal("120.000000")
    with factory() as db:
        row = db.scalar(select(InstrumentSettlementPrice))
        assert row is not None
        assert row.price_source == "YMM_DATA_SDK_LAST_TICK"
        assert row.settlement_price == Decimal("123.000000")
        assert row.previous_last_price == Decimal("120.000000")
        assert row.source_event_id == "CURRENT"
        assert row.previous_source_event_id == "PREVIOUS"


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


def test_cache_recovery_scope_includes_cash_positions_without_daily_detail():
    derivative = SimpleNamespace(
        account_id="A-SETTLE",
        exchange_id="DCE",
        symbol="JD2609",
        position_id="P-FUTURE",
        expired_closed=False,
    )
    stock = SimpleNamespace(
        account_id="A-SETTLE",
        exchange_id="SSE",
        symbol="600000",
        position_id="P-STOCK",
        total_volume=100,
    )
    closed_bond = SimpleNamespace(
        account_id="A-SETTLE",
        exchange_id="SSE",
        symbol="110001",
        position_id="P-BOND",
        total_volume=0,
    )

    affected = DailySettlementService._merge_cache_affected_positions(
        (derivative,), (stock, closed_bond)
    )

    assert {item[3] for item in affected} == {
        "P-FUTURE", "P-STOCK", "P-BOND"
    }
    assert next(item for item in affected if item[3] == "P-STOCK")[4] is False
    assert next(item for item in affected if item[3] == "P-BOND")[4] is True


def _cash_schedule_row(product_code: str, instrument_type: str = "STOCK"):
    return {
        "trading_day": TRADING_DAY,
        "exchange_id": "SSE",
        "product_code": product_code,
        "instrument_type": instrument_type,
        "sessions": [
            {
                "start_at": "2026-08-06T09:30:00+08:00",
                "end_at": "2026-08-06T15:00:00+08:00",
            }
        ],
        "status": "OPEN",
        "representative_order_book_id": "600000.XSHG",
    }


def _preflight_service(repository, instrument, *, engine, factory):
    service = DailySettlementService(
        session_factory=factory,
        database_engine=engine,
        repository=repository,
        time_provider=lambda: NOW,
        product_registry=Mock(),
        redis_recovery_enabled=False,
    )
    service._load_instruments = Mock(
        return_value=({instrument.order_book_id: instrument}, {})
    )
    return service


def _preflight_stock_instrument():
    # 股票合约表 product_id 为空，from_model 回退成 symbol。
    return SettlementInstrument(
        id=2,
        order_book_id="600033.XSHG",
        exchange_id="SSE",
        symbol="600033",
        product_id="600033",
        instrument_type="STOCK",
        multiplier=Decimal("1"),
        expire_date=None,
        last_trading_date=None,
        underlying_instrument_id=None,
        option_type=None,
        strike_price=None,
    )


def test_preflight_cash_security_falls_back_to_generic_schedule(
    sqlite_session_factory,
):
    engine, factory = sqlite_session_factory
    repository = Mock()
    repository.list_open_calendars.return_value = [
        {
            "exchange_id": "SSE",
            "trading_day": TRADING_DAY,
            "previous_trading_day": date(2026, 8, 5),
            "next_trading_day": NEXT_DAY,
            "is_open": True,
            "status": "OPEN",
        }
    ]
    repository.get_earlier_incomplete_batch.return_value = None

    def list_product_schedules(db, trading_day, product_keys):
        if ("SSE", "CASH_SECURITY", "STOCK") in set(product_keys):
            return [_cash_schedule_row("CASH_SECURITY")]
        return []

    repository.list_product_schedules.side_effect = list_product_schedules
    service = _preflight_service(
        repository, _preflight_stock_instrument(), engine=engine, factory=factory
    )

    next_day = service._preflight(TRADING_DAY)

    assert next_day == NEXT_DAY
    key_sets = [
        set(call.args[2])
        for call in repository.list_product_schedules.call_args_list
    ]
    assert any(("SSE", "600033", "STOCK") in keys for keys in key_sets)
    assert any(("SSE", "CASH_SECURITY", "STOCK") in keys for keys in key_sets)


def test_preflight_cash_security_still_fails_without_generic_schedule(
    sqlite_session_factory,
):
    engine, factory = sqlite_session_factory
    repository = Mock()
    repository.list_open_calendars.return_value = [
        {
            "exchange_id": "SSE",
            "trading_day": TRADING_DAY,
            "previous_trading_day": date(2026, 8, 5),
            "next_trading_day": NEXT_DAY,
            "is_open": True,
            "status": "OPEN",
        }
    ]
    repository.get_earlier_incomplete_batch.return_value = None
    repository.list_product_schedules.return_value = []
    service = _preflight_service(
        repository, _preflight_stock_instrument(), engine=engine, factory=factory
    )

    with pytest.raises(DailySettlementError) as exc_info:
        service._preflight(TRADING_DAY)

    assert exc_info.value.error_code == "TRADING_SCHEDULE_MISSING"


def _cancel_service(
    sqlite_session_factory, order_repository, *, derivative_service=None,
    cash_services=None,
):
    engine, factory = sqlite_session_factory
    return DailySettlementService(
        session_factory=factory,
        database_engine=engine,
        order_repository=order_repository,
        cancellation_service=derivative_service or Mock(),
        cash_security_cancellation_services=cash_services or {},
        time_provider=lambda: NOW,
        redis_recovery_enabled=False,
    )


def test_cancel_active_orders_routes_stock_order_to_cash_security_service(
    sqlite_session_factory,
):
    stock_order = SimpleNamespace(
        order_id="SO-STOCK",
        account_id="A-STOCK",
        instrument_type="STOCK",
    )
    order_repository = Mock()
    order_repository.list_active_after_id.side_effect = [[stock_order], []]
    cash_cancel = Mock()
    derivative_cancel = Mock()
    service = _cancel_service(
        sqlite_session_factory,
        order_repository,
        derivative_service=derivative_cancel,
        cash_services={"STOCK": cash_cancel},
    )

    cancelled = service._cancel_active_orders()

    assert cancelled == 1
    assert cash_cancel.cancel_order.call_count == 1
    call = cash_cancel.cancel_order.call_args
    assert call.kwargs["order_id"] == "SO-STOCK"
    assert call.kwargs["request"].account_id == "A-STOCK"
    assert call.kwargs["access_scope"].is_admin is True
    assert derivative_cancel.cancel_order.call_count == 0
    list_call = order_repository.list_active_after_id.call_args
    assert list_call.kwargs["include_cash_security"] is True


def test_cancel_active_orders_routes_futures_order_to_derivative_service(
    sqlite_session_factory,
):
    futures_order = SimpleNamespace(
        order_id="FO-FUT",
        account_id="A-FUT",
        instrument_type="FUTURES",
    )
    order_repository = Mock()
    order_repository.list_active_after_id.side_effect = [[futures_order], []]
    cash_cancel = Mock()
    derivative_cancel = Mock()
    service = _cancel_service(
        sqlite_session_factory,
        order_repository,
        derivative_service=derivative_cancel,
        cash_services={"STOCK": cash_cancel},
    )

    cancelled = service._cancel_active_orders()

    assert cancelled == 1
    assert derivative_cancel.cancel_order.call_count == 1
    call = derivative_cancel.cancel_order.call_args
    assert call.kwargs["order_id"] == "FO-FUT"
    assert call.kwargs["settlement_internal"] is True
    assert cash_cancel.cancel_order.call_count == 0
