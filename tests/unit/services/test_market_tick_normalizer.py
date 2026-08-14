from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services.market_tick_normalizer import (
    MarketTickNormalizationError,
    MarketTickNormalizer,
)
from app.schemas.market_tick_schema import MarketTickIngestType
from app.services.market_tick_validation_service import (
    MarketTickValidationError,
    MarketTickValidationService,
)


def make_data(**overrides):
    channel_overridden = "channel" in overrides
    data = {
        "action": "feed",
        "channel": "tick_AG2609",
        "order_book_id": "AG2609",
        "trading_date": pd.Timestamp("2026-07-22"),
        "datetime": pd.Timestamp("2026-07-22 09:32:08"),
        "last": 14600.0,
        "prev_close": 14396.0,
        "open": 14460.0,
        "high": 14656.0,
        "low": 14238.0,
        "volume": 24479,
        "total_turnover": 5298353100.0,
        "open_interest": 23054.0,
        "bid": [14596.0],
        "bid_vol": [1],
        "ask": [14599.0],
        "ask_vol": [2],
        "local_recv_time": pd.Timestamp("2026-07-22 09:32:07.337"),
        "sequence_id": 833,
    }
    aliases = {
        "code": "order_book_id",
        "trading_day": "trading_date",
        "event_time": "datetime",
        "last_price": "last",
        "pre_close": "prev_close",
        "cum_volume": "volume",
        "cum_turnover": "total_turnover",
    }
    for old_name, new_name in aliases.items():
        if old_name in overrides:
            overrides[new_name] = overrides.pop(old_name)
    top_fields = {
        "bid_price_1": "bid",
        "bid_volume_1": "bid_vol",
        "ask_price_1": "ask",
        "ask_volume_1": "ask_vol",
    }
    for old_name, new_name in top_fields.items():
        if old_name in overrides:
            overrides[new_name] = [overrides.pop(old_name)]
    data.update(overrides)
    if not channel_overridden:
        data["channel"] = f"tick_{data['order_book_id']}"
    return data


def make_raw(data=None, **overrides):
    raw = {
        "action": "feed",
        "channel": (data or make_data())["channel"],
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
    assert tick.source == "YMM_LIVE_DATA"


def test_pandas_timestamps_become_python_datetime_and_trading_date():
    tick = normalize()

    assert type(tick.event_time) is datetime
    assert type(tick.local_recv_time) is datetime
    assert type(tick.trading_day) is date
    assert tick.event_time.utcoffset().total_seconds() == 8 * 3600


def test_ymm_compact_integer_datetime_preserves_milliseconds():
    """生产SDK使用YYYYMMDDHHMMSSmmm整数，不能因文档示例是datetime而丢弃。"""

    tick = normalize(
        make_data(
            trading_date=20260805,
            datetime=20260805095638840,
        )
    )

    assert tick.trading_day == date(2026, 8, 5)
    assert tick.event_time == datetime(
        2026,
        8,
        5,
        9,
        56,
        38,
        840000,
        tzinfo=tick.event_time.tzinfo,
    )


@pytest.mark.parametrize(
    ("raw_value", "expected_microsecond"),
    [
        (20260805095638, 0),
        (20260805095638840, 840000),
        (20260805095638840123, 840123),
    ],
)
def test_ymm_compact_datetime_supports_seconds_millis_and_micros(
    raw_value,
    expected_microsecond,
):
    tick = normalize(make_data(datetime=raw_value))

    assert tick.event_time.microsecond == expected_microsecond


def test_aware_utc_time_is_converted_to_asia_shanghai():
    tick = normalize(
        make_data(datetime=datetime(2026, 7, 22, 1, 32, 8, tzinfo=timezone.utc))
    )

    assert tick.event_time.isoformat() == "2026-07-22T09:32:08+08:00"


def test_trading_day_comes_from_source_instead_of_event_time():
    tick = normalize(
        make_data(
            trading_date="20260723",
            datetime=datetime(2026, 7, 22, 21, 5),
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


def test_source_event_id_prefers_official_id_and_missing_sequence_is_stable():
    explicit = normalize(make_data(event_id="SOURCE-EVENT-1"))
    without_sequence = make_data()
    without_sequence.pop("sequence_id")
    first = normalize(without_sequence)
    second = normalize(dict(without_sequence))

    assert explicit.source_event_id == "SOURCE-EVENT-1"
    assert first.source_event_id == second.source_event_id
    assert first.sequence_id == second.sequence_id


def test_database_snapshot_has_stable_distinct_identity_and_source():
    data = make_data()
    first = MarketTickNormalizer().normalize(
        data=data,
        raw=make_raw(data),
        instrument=make_instrument(),
        ingest_type=MarketTickIngestType.REST_SNAPSHOT,
        source="YMM_DATA_SDK",
    )
    second = MarketTickNormalizer().normalize(
        data=dict(data),
        raw=make_raw(data),
        instrument=make_instrument(),
        ingest_type=MarketTickIngestType.REST_SNAPSHOT,
        source="YMM_DATA_SDK",
    )

    assert first.source == "YMM_DATA_SDK"
    assert first.ingest_type == MarketTickIngestType.REST_SNAPSHOT
    assert first.source_event_id.startswith("BOOTSTRAP-")
    assert first.source_event_id == second.source_event_id
    assert first.source_event_id != normalize(data).source_event_id


def test_same_prices_with_distinct_source_identity_remain_distinct_ticks():
    first = normalize(make_data(sequence_id=833))
    second = normalize(make_data(sequence_id=834))

    assert first.last_price == second.last_price
    assert first.bid_price_1 == second.bid_price_1
    assert first.source_event_id != second.source_event_id


@pytest.mark.parametrize(
    "data",
    [
        make_data(action="bar"),
        make_data(channel="bar_AG2609"),
        make_data(order_book_id=""),
    ],
)
def test_invalid_sdk_envelope_rejects_only_current_tick(data):
    with pytest.raises(MarketTickValidationError):
        MarketTickValidationService.validate_envelope(
            data=data,
            raw={},
        )


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
