from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from app.services.realtime_fact_event_service import RealtimeFactEventService


NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)


def test_account_fact_event_owns_only_postgres_decimal_fields():
    repository = Mock()
    service = RealtimeFactEventService(
        repository=repository,
        event_id_factory=lambda: "EVT-ACCOUNT",
    )
    account = SimpleNamespace(
        account_id="A001",
        account_type="FUTURES",
        cash_balance=Decimal("1000"),
        used_margin=Decimal("100"),
        option_used_margin=Decimal("60"),
        frozen_margin=Decimal("20"),
        frozen_cash=Decimal("3"),
        frozen_commission=Decimal("4"),
        used_commission=Decimal("5"),
        realized_pnl=Decimal("6"),
        unrealized_pnl=Decimal("7"),
        daily_position_pnl=Decimal("8"),
        daily_close_pnl=Decimal("9"),
        daily_commission=Decimal("10"),
        daily_pnl=Decimal("11"),
        equity=Decimal("1011"),
        available_cash=Decimal("880"),
        risk_available_cash=Decimal("870"),
        risk_ratio=Decimal("0.1"),
        risk_state="NORMAL",
        updated_at=NOW,
    )

    service.create_account_updated(Mock(), account=account, occurred_at=NOW)

    payload = repository.create_event.call_args.kwargs["payload"]
    event = repository.create_event.call_args.kwargs
    assert event["event_type"] == "ACCOUNT_FACT_UPDATED"
    assert payload["account_type"] == "FUTURES"
    assert payload["cash_balance"] == "1000"
    assert payload["frozen_commission"] == "4"
    assert payload["option_used_margin"] == "60"
    assert payload["daily_close_pnl"] == "9"
    assert all(
        not isinstance(payload[field], float)
        for field in (
            "cash_balance",
            "used_margin",
            "daily_close_pnl",
            "daily_commission",
        )
    )
    assert {
        "unrealized_pnl",
        "daily_position_pnl",
        "daily_pnl",
        "equity",
        "available_cash",
        "risk_available_cash",
        "risk_ratio",
        "risk_state",
    }.isdisjoint(payload)


def test_zero_volume_position_produces_closed_absolute_event():
    repository = Mock()
    service = RealtimeFactEventService(
        repository=repository,
        event_id_factory=lambda: "EVT-POSITION",
    )
    position = SimpleNamespace(
        position_id="P001",
        account_id="A001",
        exchange_id="DCE",
        symbol="JD2609",
        order_book_id="JD2609",
        instrument_type="FUTURES",
        direction="LONG",
        total_volume=0,
        today_volume=0,
        yesterday_volume=0,
        available_volume=0,
        frozen_volume=0,
        average_open_price=Decimal("3500"),
        position_cost=Decimal("0"),
        used_margin=Decimal("0"),
        realtime_required_margin=Decimal("0"),
        realized_pnl=Decimal("20"),
        unrealized_pnl=Decimal("0"),
        daily_position_pnl=Decimal("0"),
        daily_close_pnl=Decimal("20"),
        trading_day=date(2026, 8, 4),
        updated_at=NOW,
    )

    service.create_position_updated(
        Mock(),
        position=position,
        occurred_at=NOW,
        fact_reason="OPTION_MARGIN_ADJUSTMENT",
    )

    event = repository.create_event.call_args.kwargs
    assert event["event_type"] == "POSITION_CLOSED"
    assert event["payload"]["total_volume"] == 0
    assert event["payload"]["used_margin"] == "0"
    assert event["payload"]["realtime_required_margin"] == "0"
    assert event["payload"]["fact_reason"] == "OPTION_MARGIN_ADJUSTMENT"


def test_order_margin_fact_contains_absolute_decimal_values():
    repository = Mock()
    service = RealtimeFactEventService(
        repository=repository,
        event_id_factory=lambda: "EVT-ORDER-MARGIN",
    )
    order = SimpleNamespace(
        order_id="O001",
        client_order_id="C001",
        account_id="A001",
        exchange_id="DCE",
        symbol="JD2609-C-4000",
        order_book_id="JD2609-C-4000",
        trading_day=date(2026, 8, 4),
        instrument_type="FUTURES_OPTION",
        direction="SELL",
        offset_flag="OPEN",
        order_type="LIMIT",
        limit_price=Decimal("100.5"),
        total_volume=3,
        traded_volume=0,
        remaining_volume=3,
        cancelled_volume=0,
        average_price=None,
        status="ACCEPTED",
        submit_status="ACCEPTED",
        frozen_margin=Decimal("11781.9"),
        frozen_cash=Decimal("0"),
        frozen_commission=Decimal("3"),
        frozen_position_volume=0,
        margin_price_mode="REALTIME",
        margin_underlying_price=Decimal("4033"),
        margin_option_price=Decimal("105.5"),
        margin_calculation_version="OPTION_MARGIN_V1",
        margin_risk_state="NORMAL",
        accepted_at=NOW,
        cancelled_at=None,
        updated_at=NOW,
    )

    service.create_order_margin_updated(
        Mock(), order=order, occurred_at=NOW
    )

    event = repository.create_event.call_args.kwargs
    payload = event["payload"]
    assert event["event_type"] == "ORDER_MARGIN_UPDATED"
    assert event["aggregate_type"] == "ORDER"
    assert payload["frozen_margin"] == "11781.9"
    assert payload["margin_underlying_price"] == "4033"
    assert payload["margin_option_price"] == "105.5"
    assert payload["margin_risk_state"] == "NORMAL"
    assert not any(
        isinstance(value, float)
        for value in payload.values()
    )
