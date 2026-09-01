from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from app.infrastructure.market_data.database_snapshot_client import (
    DatabaseSnapshotConfigurationError,
)
from app.infrastructure.market_data.historical_price_client import (
    YmmHistoricalPriceClient,
)


def make_config(**overrides):
    values = {
        "ymm_data_sdk_token": "database-token",
        "ymm_data_sdk_mode": "lan",
        "remote_market_data_mode": "TS",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_local_mode_allows_empty_token():
    sdk = Mock()
    sdk.get_price.return_value = pd.DataFrame(
        [{"datetime": "2026-08-31", "open": 1, "high": 2, "low": 1, "close": 2}]
    )
    client = YmmHistoricalPriceClient(
        make_config(ymm_data_sdk_token="", ymm_data_sdk_mode="local"),
        sdk_module=sdk,
    )

    result = client.fetch_daily_bars(
        "000001.XSHE",
        start_date=pd.Timestamp("2026-08-31").date(),
        end_date=pd.Timestamp("2026-08-31").date(),
    )

    sdk.init.assert_called_once_with(token=None, mode="local")
    assert result[0]["close"] == 2


def test_remote_mode_requires_token():
    with pytest.raises(DatabaseSnapshotConfigurationError, match="YMM_DATA_SDK_TOKEN"):
        YmmHistoricalPriceClient(
            make_config(ymm_data_sdk_token="", ymm_data_sdk_mode="TS"),
            sdk_module=Mock(),
        )
