from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.common.exceptions import BusinessRuleError
from app.services.trading_day_service import TradingDayService


SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeRepository:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def list_candidate_schedules(self, _db, **_kwargs):
        self.calls += 1
        return self.rows

    def list_cash_security_candidate_schedules(self, _db, **_kwargs):
        self.calls += 1
        return self.rows

    def list_account_candidate_schedules(self, _db, **_kwargs):
        self.calls += 1
        return self.rows


def instrument():
    return SimpleNamespace(
        exchange_id="CZCE",
        product_id="CF",
        instrument_type="FUTURES",
    )


def row(*, trading_day=date(2026, 8, 11), allow_open=True):
    return {
        "trading_day": trading_day,
        "calendar_is_open": True,
        "calendar_status": "OPEN",
        "schedule_status": "OPEN",
        "sessions": [
            {
                "start_at": "2026-08-10T21:00:00+08:00",
                "end_at": "2026-08-10T23:00:00+08:00",
                "allow_open": allow_open,
                "allow_close": True,
            }
        ],
    }


def test_night_session_resolves_to_next_trading_day_and_uses_cache():
    repository = FakeRepository([row()])
    service = TradingDayService(repository=repository)
    now = datetime(2026, 8, 10, 21, 30, tzinfo=SHANGHAI)

    first = service.resolve_for_order(
        object(), instrument=instrument(), offset_flag="OPEN", now=now
    )
    second = service.resolve_for_order(
        object(), instrument=instrument(), offset_flag="OPEN", now=now
    )

    assert first == second == date(2026, 8, 11)
    assert repository.calls == 1


def test_open_order_obeys_session_permission():
    service = TradingDayService(repository=FakeRepository([row(allow_open=False)]))

    with pytest.raises(BusinessRuleError) as exc_info:
        service.resolve_for_order(
            object(),
            instrument=instrument(),
            offset_flag="OPEN",
            now=datetime(2026, 8, 10, 21, 30, tzinfo=SHANGHAI),
        )

    assert exc_info.value.error_code == "OUTSIDE_TRADING_SESSION"


def test_missing_schedule_fails_closed():
    service = TradingDayService(repository=FakeRepository([]))

    with pytest.raises(BusinessRuleError) as exc_info:
        service.resolve_for_order(
            object(),
            instrument=instrument(),
            offset_flag="OPEN",
            now=datetime(2026, 8, 10, 21, 30, tzinfo=SHANGHAI),
        )

    assert exc_info.value.error_code == "TRADING_SCHEDULE_MISSING"


def test_closed_calendar_is_not_accepted():
    closed = row()
    closed["calendar_is_open"] = False
    service = TradingDayService(repository=FakeRepository([closed]))

    with pytest.raises(BusinessRuleError) as exc_info:
        service.resolve_for_order(
            object(),
            instrument=instrument(),
            offset_flag="OPEN",
            now=datetime(2026, 8, 10, 21, 30, tzinfo=SHANGHAI),
        )

    assert exc_info.value.error_code == "TRADING_SCHEDULE_MISSING"


def test_cash_security_trading_day_does_not_require_product_id():
    repository = FakeRepository([row(trading_day=date(2026, 8, 10))])
    service = TradingDayService(repository=repository)
    cash_instrument = SimpleNamespace(
        exchange_id="SSE",
        product_id=None,
        instrument_type="CONVERTIBLE_BOND",
    )
    now = datetime(2026, 8, 10, 21, 30, tzinfo=SHANGHAI)

    result = service.resolve_for_cash_security_order(
        object(), instrument=cash_instrument, now=now
    )

    assert result == date(2026, 8, 10)
    assert repository.calls == 1


def test_account_creation_uses_active_session_trading_day():
    repository = FakeRepository([row()])
    service = TradingDayService(repository=repository)

    result = service.resolve_for_account_creation(
        object(),
        account_type="FUTURES",
        now=datetime(2026, 8, 10, 21, 30, tzinfo=SHANGHAI),
    )

    assert result == date(2026, 8, 11)


def test_account_creation_during_break_uses_next_session():
    next_session = row(trading_day=date(2026, 8, 11))
    next_session["sessions"][0]["start_at"] = "2026-08-11T09:30:00+08:00"
    next_session["sessions"][0]["end_at"] = "2026-08-11T11:30:00+08:00"
    service = TradingDayService(repository=FakeRepository([next_session]))

    result = service.resolve_for_account_creation(
        object(),
        account_type="SECURITIES_CASH",
        now=datetime(2026, 8, 10, 16, 0, tzinfo=SHANGHAI),
    )

    assert result == date(2026, 8, 11)
