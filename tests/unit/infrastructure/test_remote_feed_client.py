import os
import logging
from types import SimpleNamespace
from unittest.mock import Mock

from app.infrastructure.market_data.remote_feed_client import (
    RemoteFeedClient,
    create_remote_sdk_client,
)


def test_sdk_client_is_created_from_settings_without_logging_credentials(caplog):
    client_class = Mock()
    config = SimpleNamespace(
        remote_market_data_base_url="https://market.example.test",
        remote_market_data_timeout_seconds=4.5,
        remote_market_data_api_user="private-user",
        remote_market_data_api_token="private-token",
        remote_market_data_verify_ssl=False,
    )

    with caplog.at_level(logging.DEBUG):
        create_remote_sdk_client(config, client_class=client_class)

    client_class.assert_called_once_with(
        base_url="https://market.example.test",
        timeout=4.5,
        api_user="private-user",
        api_token="private-token",
        verify_ssl=False,
    )
    assert "private-user" not in caplog.text
    assert "private-token" not in caplog.text


def test_start_callbacks_uses_latest_tick_parameters(monkeypatch):
    sdk = Mock()
    sdk.use_env_proxy = False
    sdk.base_url = "http://feedhub.internal.test:54111"
    client = RemoteFeedClient(sdk)
    callbacks = {
        "on_quote": Mock(),
        "on_subscribe": Mock(),
        "on_message": Mock(),
        "on_error": Mock(),
    }

    client.start_tick_callbacks({"AU2608", "AG2609"}, **callbacks)

    sdk.start_quote_callbacks.assert_called_once_with(
        ["AG2609", "AU2608"],
        freq="tick",
        daemon=False,
        **callbacks,
    )
    assert "feedhub.internal.test" in os.environ["NO_PROXY"].split(",")


def test_latest_snapshot_requests_raw_dict():
    sdk = Mock()
    client = RemoteFeedClient(sdk)

    client.get_latest_ticks({"AU2608", "AG2609"})

    sdk.get_latest_ticks.assert_called_once_with(
        ["AG2609", "AU2608"],
        expect_df=False,
    )
