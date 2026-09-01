from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from app.infrastructure.market_data.settlement_last_tick_provider import (
    SettlementLastTickDataError,
    YmmSettlementLastTickProvider,
)


DAY = date(2026, 8, 13)
PREVIOUS_DAY = date(2026, 8, 12)


class FakeSdk:
    def __init__(self, frame):
        self.frame = frame
        self.init_calls = []
        self.price_calls = []

    def init(self, **kwargs):
        self.init_calls.append(kwargs)

    @staticmethod
    def get_previous_trading_date(value, *, n, market):
        assert value == DAY
        assert n == 1
        assert market == "cn"
        return PREVIOUS_DAY

    def get_price(self, codes, **kwargs):
        self.price_calls.append((codes, kwargs))
        return self.frame


def _config():
    return SimpleNamespace(
        ymm_data_sdk_token="test-token",
        remote_market_data_mode="lan",
        remote_market_data_timeout_seconds=0,
    )


def _frame(rows):
    return pd.DataFrame(
        rows,
        columns=["order_book_id", "datetime", "trading_date", "last"],
    ).set_index(["order_book_id", "datetime"])


def test_fetch_many_uses_last_tick_of_current_and_previous_trading_day():
    sdk = FakeSdk(
        _frame(
            [
                ("JD2608", "2026-08-13 15:03:37.730", DAY, 4470),
                ("JD2608", "2026-08-12 15:04:22.512", PREVIOUS_DAY, 4470),
                ("JD2608", "2026-08-13 09:00:00.011", DAY, 4460),
                ("JD2608", "2026-08-12 09:00:00.011", PREVIOUS_DAY, 4500),
            ]
        )
    )
    provider = YmmSettlementLastTickProvider(_config(), sdk_module=sdk)

    pair = provider.fetch_many(["jd2608"], DAY)["JD2608"]

    assert pair.current.last_price.as_tuple().exponent == -6
    assert pair.current.last_price == 4470
    assert pair.current.event_time.isoformat() == "2026-08-13T15:03:37.730000+08:00"
    assert pair.previous is not None
    assert pair.previous.last_price == 4470
    assert pair.previous.event_time.isoformat() == (
        "2026-08-12T15:04:22.512000+08:00"
    )
    assert sdk.price_calls == [
        (
            ["JD2608"],
            {
                "start_date": "2026-08-12",
                "end_date": "2026-08-13",
                "frequency": "tick",
                "fields": ["last", "trading_date"],
                "adjust_type": "none",
            },
        )
    ]


def test_fetch_many_allows_missing_previous_tick_for_new_contract():
    sdk = FakeSdk(
        _frame([("JD2608", "2026-08-13 15:03:37.730", DAY, 4470)])
    )

    pair = YmmSettlementLastTickProvider(
        _config(), sdk_module=sdk
    ).fetch_many(["JD2608"], DAY)["JD2608"]

    assert pair.previous is None


def test_local_mode_allows_empty_token_and_uses_dedicated_mode():
    sdk = FakeSdk(
        _frame([("JD2608", "2026-08-13 15:03:37.730", DAY, 4470)])
    )
    config = SimpleNamespace(
        ymm_data_sdk_token="",
        ymm_data_sdk_mode="local",
        remote_market_data_mode="lan",
        remote_market_data_timeout_seconds=0,
    )

    YmmSettlementLastTickProvider(
        config, sdk_module=sdk
    ).fetch_many(["JD2608"], DAY)

    assert sdk.init_calls == [{"token": None, "mode": "local"}]


def test_fetch_many_rejects_missing_current_tick():
    sdk = FakeSdk(
        _frame(
            [("JD2608", "2026-08-12 15:04:22.512", PREVIOUS_DAY, 4470)]
        )
    )

    with pytest.raises(SettlementLastTickDataError, match="当日最后 Tick 不存在"):
        YmmSettlementLastTickProvider(_config(), sdk_module=sdk).fetch_many(
            ["JD2608"], DAY
        )


def test_fetch_many_preserves_sdk_running_reason_after_retry_deadline():
    class YMMDataUnavailableError(RuntimeError):
        pass

    sdk = FakeSdk(None)

    def unavailable(*args, **kwargs):
        raise YMMDataUnavailableError(
            "live data is unavailable for 2026-08-14 future_tick: running"
        )

    sdk.get_price = unavailable

    with pytest.raises(SettlementLastTickDataError, match="future_tick: running"):
        YmmSettlementLastTickProvider(_config(), sdk_module=sdk).fetch_many(
            ["JD2608"], DAY
        )
