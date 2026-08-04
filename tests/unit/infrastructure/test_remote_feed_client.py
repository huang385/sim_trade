import logging
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.infrastructure.market_data.remote_feed_client import (
    RemoteFeedClient,
    RemoteMarketDataConfigurationError,
    RemoteMarketDataSdkUnavailableError,
    create_remote_sdk_client,
)


class FakeThread:
    def __init__(self):
        self.alive = True
        self.join_calls = []

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)


class FakeSdk:
    def __init__(self):
        self.subscriptions = []
        self.feed_handler = None
        self.status_handler = None
        self.feed_thread = FakeThread()
        self.status_thread = FakeThread()
        self.closed = False

    def listen(self, handler):
        self.feed_handler = handler
        return self.feed_thread

    def listen_status(self, handler):
        self.status_handler = handler
        return self.status_thread

    def subscribe(self, channels):
        self.subscriptions = sorted(set(self.subscriptions) | set(channels))

    def unsubscribe(self, channels):
        self.subscriptions = sorted(set(self.subscriptions) - set(channels))

    def close(self):
        self.closed = True
        self.feed_thread.alive = False
        self.status_thread.alive = False


def make_config(**overrides):
    values = {
        "remote_market_data_api_token": "private-token",
        "remote_market_data_mode": "lan",
        "remote_market_data_base_url": "",
        "remote_market_data_ca_file": "",
        "remote_market_data_verify_ssl": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_sdk_client_is_created_from_settings_without_logging_credentials(caplog):
    client_class = Mock()
    config = make_config(
        remote_market_data_base_url="wss://market.example.test",
        remote_market_data_ca_file="/safe/ca.pem",
    )

    with caplog.at_level(logging.DEBUG):
        create_remote_sdk_client(config, client_class=client_class)

    client_class.assert_called_once_with(
        token="private-token",
        mode="lan",
        server_url="wss://market.example.test",
        ca_file="/safe/ca.pem",
    )
    assert "private-token" not in caplog.text
    assert "market.example.test" not in caplog.text


@pytest.mark.parametrize(
    "config",
    [
        make_config(remote_market_data_api_token=""),
        make_config(remote_market_data_mode="", remote_market_data_base_url=""),
        make_config(remote_market_data_verify_ssl=False),
    ],
)
def test_missing_or_unsafe_configuration_fails_explicitly(config):
    with pytest.raises(RemoteMarketDataConfigurationError):
        create_remote_sdk_client(config, client_class=Mock())


def test_missing_official_sdk_reports_exact_install_requirement(monkeypatch):
    original_import = __import__

    def import_without_sdk(name, *args, **kwargs):
        if name == "ymm_live_data_sdk":
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(
        "builtins.__import__",
        import_without_sdk,
    )
    with pytest.raises(
        RemoteMarketDataSdkUnavailableError,
        match="ymm-live-data-sdk==0.4.0",
    ):
        create_remote_sdk_client(make_config())


def test_listeners_start_before_batch_subscription_and_tick_is_copied():
    sdk = FakeSdk()
    events = []
    quotes = []
    client = RemoteFeedClient(sdk)

    subscription = client.start_tick_callbacks(
        {"AU2608", "AG2609"},
        on_quote=lambda data, raw: quotes.append((data, raw)),
        on_subscribe=lambda report: events.append(report),
        on_message=lambda message: events.append(message),
        on_error=lambda error: events.append(error),
    )

    assert sdk.feed_handler is not None
    assert sdk.status_handler is not None
    assert sdk.subscriptions == ["tick_AG2609", "tick_AU2608"]
    assert set(events[0]["contracts"]) == {"AG2609", "AU2608"}

    message = {
        "action": "feed",
        "channel": "tick_AG2609",
        "order_book_id": "AG2609",
    }
    sdk.feed_handler(message)
    message["order_book_id"] = "MUTATED"
    assert quotes[0][0]["order_book_id"] == "AG2609"
    assert subscription.is_alive()


def test_incremental_subscribe_unsubscribe_uses_public_sdk_methods():
    sdk = FakeSdk()
    sdk.subscribe = Mock(wraps=sdk.subscribe)
    sdk.unsubscribe = Mock(wraps=sdk.unsubscribe)
    client = RemoteFeedClient(sdk)
    client.start_tick_callbacks(
        {"AG2609"},
        on_quote=Mock(),
        on_subscribe=Mock(),
        on_message=Mock(),
        on_error=Mock(),
    )

    report = client.replace_tick_subscriptions({"AG2609", "AU2608"})
    sdk.subscribe.assert_called_with(["tick_AU2608"])
    sdk.unsubscribe.assert_not_called()
    assert report["contracts"]["AU2608"]["subscribed"] is True

    client.replace_tick_subscriptions({"AU2608"})
    sdk.unsubscribe.assert_called_once_with(["tick_AG2609"])


@dataclass(frozen=True)
class FakeStatus:
    component: str
    state: str
    message: str
    details: dict
    timestamp: str = "2026-08-04T09:00:00Z"
    sequence: int = 7


def test_status_is_sanitized_and_connection_error_is_reported():
    sdk = FakeSdk()
    messages = []
    errors = []
    client = RemoteFeedClient(sdk)
    client.start_tick_callbacks(
        {"AG2609"},
        on_quote=Mock(),
        on_subscribe=Mock(),
        on_message=messages.append,
        on_error=errors.append,
    )

    sdk.status_handler(
        FakeStatus(
            component="hub",
            state="reconnecting",
            message="temporary disconnect",
            details={"reason": "network", "token": "must-not-leak"},
        )
    )

    assert messages[0]["details"] == {"reason": "network"}
    assert errors[0] == {"raw": {"code": "HUB_RECONNECTING"}}
    assert "must-not-leak" not in str(messages)


def test_stop_closes_sdk_and_joins_both_threads():
    sdk = FakeSdk()
    client = RemoteFeedClient(sdk)
    subscription = client.start_tick_callbacks(
        {"AG2609"},
        on_quote=Mock(),
        on_subscribe=Mock(),
        on_message=Mock(),
        on_error=Mock(),
    )

    subscription.stop()
    subscription.join(timeout=4)

    assert sdk.closed is True
    assert not subscription.is_alive()
    assert sdk.feed_thread.join_calls == [2]
    assert sdk.status_thread.join_calls == [2]
