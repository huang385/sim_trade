from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from app.infrastructure.market_data.database_snapshot_client import (
    YmmDatabaseSnapshotClient,
)


def make_config(**overrides):
    values = {
        "ymm_data_sdk_token": "private-database-token",
        "remote_market_data_mode": "lan",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_snapshot(
    code: str,
    *,
    available: bool = True,
    last: int = 100,
    event_time: datetime | None = None,
):
    return SimpleNamespace(
        available=available,
        order_book_id=code,
        datetime=event_time or datetime(2026, 8, 13, 9, 1, 0, 2000),
        last=last,
        prev_close=99,
        open=100,
        high=102,
        low=98,
        volume=2,
        total_turnover=202,
        open_interest=30,
        asks=[101, 102, 103, 104, 105],
        ask_vols=[1, 2, 3, 4, 5],
        bids=[100, 99, 98, 97, 96],
        bid_vols=[6, 7, 8, 9, 10],
    )


def test_fetch_latest_many_uses_current_snapshot_and_converts_ticks():
    sdk = Mock()
    sdk.get_future_latest_trading_date.return_value = "2026-08-13"
    sdk.current_snapshot.return_value = [
        make_snapshot("AG2612", last=101),
        make_snapshot("JD2608", last=200),
    ]
    client = YmmDatabaseSnapshotClient(make_config(), sdk_module=sdk)

    result = client.fetch_latest_many(["JD2608", "AG2612", "AG2612"])

    sdk.init.assert_called_once_with(token="private-database-token", mode="lan")
    sdk.current_snapshot.assert_called_once_with(["AG2612", "JD2608"])
    sdk.get_price.assert_not_called()
    assert result["AG2612"]["datetime"] == datetime(
        2026, 8, 13, 9, 1, 0, 2000
    )
    assert result["AG2612"]["trading_date"] == "2026-08-13"
    assert result["AG2612"]["last"] == 101
    assert result["AG2612"]["ask"][0] == 101
    assert result["JD2608"]["bid_vol"][0] == 6


def test_unavailable_snapshot_is_not_published():
    sdk = Mock()
    sdk.get_future_latest_trading_date.return_value = "2026-08-13"
    sdk.current_snapshot.return_value = [
        make_snapshot("JD2608", available=False),
    ]
    client = YmmDatabaseSnapshotClient(make_config(), sdk_module=sdk)

    assert client.fetch_latest_many(["JD2608"]) == {}


def test_single_snapshot_response_is_supported():
    sdk = Mock()
    sdk.get_future_latest_trading_date.return_value = "2026-08-13"
    sdk.current_snapshot.return_value = make_snapshot("JD2608", last=200)
    client = YmmDatabaseSnapshotClient(make_config(), sdk_module=sdk)

    result = client.fetch_latest_many(["JD2608"])

    assert result["JD2608"]["last"] == 200
