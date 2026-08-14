from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from app.infrastructure.market_data.database_snapshot_client import (
    YmmDatabaseSnapshotClient,
)


def make_config(**overrides):
    values = {
        "ymm_data_sdk_token": "private-database-token",
        "remote_market_data_mode": "lan",
        "remote_market_data_timeout_seconds": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_fetch_latest_many_returns_last_tick_per_contract():
    sdk = Mock()
    sdk.get_future_latest_trading_date.return_value = pd.Timestamp("2026-08-13")
    index = pd.MultiIndex.from_tuples(
        [
            ("AG2612", pd.Timestamp("2026-08-13 09:00:00.001")),
            ("AG2612", pd.Timestamp("2026-08-13 09:01:00.002")),
            ("JD2608", pd.Timestamp("2026-08-13 09:00:30.003")),
        ],
        names=["order_book_id", "datetime"],
    )
    sdk.get_price.return_value = pd.DataFrame(
        {
            "trading_date": [pd.Timestamp("2026-08-13")] * 3,
            "last": [100, 101, 200],
            "volume": [1, 2, 3],
            "total_turnover": [100, 202, 600],
            "a1": [102, 103, 201],
            "a1_v": [1, 2, 3],
            "b1": [99, 100, 199],
            "b1_v": [4, 5, 6],
        },
        index=index,
    )
    client = YmmDatabaseSnapshotClient(make_config(), sdk_module=sdk)

    result = client.fetch_latest_many(["JD2608", "AG2612", "AG2612"])

    sdk.init.assert_called_once_with(token="private-database-token", mode="lan")
    sdk.get_price.assert_called_once_with(
        ["AG2612", "JD2608"],
        start_date="2026-08-13",
        end_date="2026-08-13",
        frequency="tick",
        fields=None,
        adjust_type="none",
    )
    assert result["AG2612"]["datetime"] == pd.Timestamp(
        "2026-08-13 09:01:00.002"
    )
    assert result["AG2612"]["last"] == 101
    assert result["AG2612"]["ask"][0] == 103
    assert result["JD2608"]["bid_vol"][0] == 6


def test_empty_database_result_returns_empty_mapping():
    sdk = Mock()
    sdk.get_future_latest_trading_date.return_value = "2026-08-13"
    sdk.get_price.return_value = pd.DataFrame()
    client = YmmDatabaseSnapshotClient(make_config(), sdk_module=sdk)

    assert client.fetch_latest_many(["JD2608"]) == {}


def test_temporary_running_status_retries_within_configured_timeout():
    class YMMDataUnavailableError(RuntimeError):
        pass

    sdk = Mock()
    sdk.get_future_latest_trading_date.return_value = "2026-08-13"
    index = pd.MultiIndex.from_tuples(
        [("JD2608", pd.Timestamp("2026-08-13 09:00:30.003"))],
        names=["order_book_id", "datetime"],
    )
    frame = pd.DataFrame(
        {"trading_date": [pd.Timestamp("2026-08-13")], "last": [200]},
        index=index,
    )
    sdk.get_price.side_effect = [
        YMMDataUnavailableError("live data is unavailable: running"),
        frame,
    ]
    clock = Mock(side_effect=[0.0, 0.0, 0.0])
    sleeper = Mock()
    client = YmmDatabaseSnapshotClient(
        make_config(),
        sdk_module=sdk,
        monotonic=clock,
        sleep=sleeper,
    )

    result = client.fetch_latest_many(["JD2608"])

    assert result["JD2608"]["last"] == 200
    assert sdk.get_price.call_count == 2
    sleeper.assert_called_once_with(0.25)
