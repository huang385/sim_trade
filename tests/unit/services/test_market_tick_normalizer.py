from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services.market_tick_normalizer import (
    MarketTickNormalizationError,
    MarketTickNormalizer,
)
from app.services.market_tick_validation_service import (
    MarketTickValidationService,
)


def make_data(**overrides):
    data = {
        "code": "AG2609",
        "exchange": "SHFE",
        "trading_day": pd.Timestamp("2026-07-22"),
        "last_price": 14600.0,
        "pre_close": 14396.0,
        "open": 14460.0,
        "high": 14656.0,
        "low": 14238.0,
        "cum_volume": 24479,
        "cum_turnover": 5298353100.0,
        "open_interest": 23054.0,
        "bid_price_1": 14596.0,
        "bid_volume_1": 1,
        "ask_price_1": 14599.0,
        "ask_volume_1": 2,
        "raw_update_time": "09:32:08",
        "raw_update_millisec": 0,
        "event_time": pd.Timestamp("2026-07-22 09:32:08"),
        "local_recv_time": pd.Timestamp("2026-07-22 09:32:07.337"),
        "sequence_id": 833,
    }
    data.update(overrides)
    return data


def make_raw(data=None, **overrides):
    raw = {
        "type": "tick",
        "event": "update",
        "channel": "tick.AG2609",
        "data": data or make_data(),
        "server_time": pd.Timestamp("2026-07-22 09:32:07.337"),
    }
    raw.update(overrides)
    return raw


def make_instrument(**overrides):
    values = {
        "order_book_id": "AG2609",
        "exchange_id": "SHFE",
        "symbol": "AG2609",
        "is_active": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def normalize(data=None, instrument=None):
    data = data or make_data()
    return MarketTickNormalizer().normalize(
        data=data,
        raw=make_raw(data),
        instrument=instrument or make_instrument(),
    )


def test_real_tick_fields_are_mapped_without_float_decimal_round_trip():
    tick = normalize()

    assert tick.order_book_id == "AG2609"
    assert tick.symbol == "AG2609"
    assert tick.open_price == Decimal("14460.0")
    assert tick.high_price == Decimal("14656.0")
    assert tick.low_price == Decimal("14238.0")
    assert tick.cumulative_volume == 24479
    assert tick.cumulative_turnover == Decimal("5298353100.0")
    assert tick.bid_volume_1 == 1
    assert tick.ask_volume_1 == 2


def test_pandas_timestamps_become_python_datetime_and_trading_date():
    tick = normalize()

    assert type(tick.event_time) is datetime
    assert type(tick.local_recv_time) is datetime
    assert type(tick.trading_day) is date
    assert tick.event_time.utcoffset().total_seconds() == 8 * 3600


def test_aware_utc_time_is_converted_to_asia_shanghai():
    tick = normalize(
        make_data(event_time=datetime(2026, 7, 22, 1, 32, 8, tzinfo=timezone.utc))
    )

    assert tick.event_time.isoformat() == "2026-07-22T09:32:08+08:00"


def test_trading_day_comes_from_source_instead_of_event_time():
    tick = normalize(
        make_data(
            trading_day="20260723",
            event_time=datetime(2026, 7, 22, 21, 5),
        )
    )

    assert tick.trading_day == date(2026, 7, 23)
    assert tick.event_time.date() == date(2026, 7, 22)


def test_local_receive_time_before_event_time_is_valid():
    tick = normalize()

    MarketTickValidationService.validate(
        tick=tick,
        instrument=make_instrument(),
    )
    assert tick.local_recv_time < tick.event_time


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "-Infinity", "abc"])
def test_invalid_non_finite_price_is_rejected(value):
    with pytest.raises(MarketTickNormalizationError):
        normalize(make_data(last_price=value))


def test_source_event_id_is_stable_for_same_identity_fields():
    assert normalize().source_event_id == normalize().source_event_id


def test_crossed_top_of_book_is_trusted_to_upstream_source():
    tick = normalize(make_data(bid_price_1=14601, ask_price_1=14599))

    MarketTickValidationService.validate(
        tick=tick,
        instrument=make_instrument(),
    )


def test_exchange_consistency_is_trusted_to_upstream_source():
    tick = normalize()

    MarketTickValidationService.validate(
        tick=tick,
        instrument=make_instrument(exchange_id="DCE"),
    )


def test_negative_integer_and_decimal_values_are_not_revalidated_locally():
    tick = normalize(
        make_data(
            last_price=-1,
            cum_volume=-2,
            bid_volume_1=-3,
            ask_volume_1=-4,
        )
    )

    MarketTickValidationService.validate(
        tick=tick,
        instrument=make_instrument(),
    )


def test_one_sided_quote_is_allowed():
    tick = normalize(make_data(bid_price_1=None, ask_price_1=14599))

    MarketTickValidationService.validate(
        tick=tick,
        instrument=make_instrument(),
    )
