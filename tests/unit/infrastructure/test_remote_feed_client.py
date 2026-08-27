import logging
import threading
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
    """按 SDK 0.8.5 接口建模：listen 为阻塞式关键字参数消费循环。"""

    def __init__(self):
        self.subscriptions = []
        self.feed_handler = None
        self.status_handler = None
        self.status_thread = FakeThread()
        self.closed = False
        self.feed_started = threading.Event()
        self._feed_stop = threading.Event()

    def listen(self, *, tick_handler=None, bar_handler=None):
        self.feed_handler = tick_handler
        self.feed_started.set()
        self._feed_stop.wait()
        return None

    def listen_status(self, handler):
        self.status_handler = handler
        return self.status_thread

    def subscribe(self, channels):
        self.subscriptions = sorted(set(self.subscriptions) | set(channels))

    def unsubscribe(self, channels):
        self.subscriptions = sorted(set(self.subscriptions) - set(channels))

    def close(self):
        self.closed = True
        self._feed_stop.set()
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
        match="ymm-live-data-sdk==0.8.5",
    ):
        create_remote_sdk_client(make_config())


def test_batch_subscription_then_blocking_feed_listener_with_tick_copy():
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

    assert sdk.feed_started.wait(timeout=2)
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


def test_batch_tick_callback_is_expanded_to_individual_quotes():
    sdk = FakeSdk()
    quotes = []
    errors = []
    client = RemoteFeedClient(sdk)
    client.start_tick_callbacks(
        {"AG2609", "AU2608"},
        on_quote=lambda data, raw: quotes.append((data, raw)),
        on_subscribe=Mock(),
        on_message=Mock(),
        on_error=errors.append,
    )

    assert sdk.feed_started.wait(timeout=2)
    sdk.feed_handler(
        (
            {
                "action": "feed",
                "channel": "tick_AG2609",
                "order_book_id": "AG2609",
            },
            {
                "action": "feed",
                "channel": "tick_AU2608",
                "order_book_id": "AU2608",
            },
        )
    )

    assert [item[0]["order_book_id"] for item in quotes] == [
        "AG2609",
        "AU2608",
    ]
    assert errors == []


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


def test_storage_slow_consumer_is_not_forwarded_twice_as_error():
    sdk = FakeSdk()
    messages = []
    errors = []
    client = RemoteFeedClient(sdk)
    client.start_tick_callbacks(
        {"JD2609"},
        on_quote=Mock(),
        on_subscribe=Mock(),
        on_message=messages.append,
        on_error=errors.append,
    )

    sdk.status_handler(
        FakeStatus(
            component="storage",
            state="slow_consumer",
            message="storage queue is slow",
            details={},
        )
    )

    assert messages[0]["component"] == "storage"
    assert messages[0]["state"] == "slow_consumer"
    assert errors == []


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
    assert sdk.status_thread.join_calls == [2]

def test_immediate_listen_failure_is_raised_and_client_is_closed():
    sdk = FakeSdk()

    def failing_listen(*, tick_handler=None, bar_handler=None):
        raise RuntimeError("subscribed lanes require consumers")

    sdk.listen = failing_listen
    client = RemoteFeedClient(sdk)

    with pytest.raises(RuntimeError, match="subscribed lanes require consumers"):
        client.start_tick_callbacks(
            {"AG2609"},
            on_quote=Mock(),
            on_subscribe=Mock(),
            on_message=Mock(),
            on_error=Mock(),
        )

    assert sdk.closed is True

