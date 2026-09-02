import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "futures_market_tick_stream_name": "same",
                "securities_market_tick_stream_name": "same",
            },
            "不能共用 Tick Stream",
        ),
        (
            {
                "futures_market_source_status_key": "same",
                "securities_market_source_status_key": "same",
            },
            "不能共用状态键",
        ),
        (
            {
                "futures_matching_consumer_group": "same",
                "securities_matching_consumer_group": "same",
            },
            "不能共用撮合 Consumer Group",
        ),
        (
            {
                "futures_market_data_api_token": "same-token",
                "securities_market_data_api_token": "same-token",
            },
            "不能共用同一个行情 Token",
        ),
    ],
)
def test_market_domain_resources_must_be_distinct(overrides, message):
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, debug=False, **overrides)
