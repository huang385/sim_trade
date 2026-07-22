import os
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from app.core.config import Settings, settings


def create_remote_sdk_client(
    config: Settings = settings,
    *,
    client_class=None,
):
    """按Settings创建外部SDK对象；禁止记录地址中的鉴权信息和凭证。"""

    if client_class is None:
        from app.infrastructure.market_data.vendor.remote_sdk_client import (
            RemoteMarketDataClient,
        )

        client_class = RemoteMarketDataClient
    return client_class(
        base_url=config.remote_market_data_base_url,
        timeout=config.remote_market_data_timeout_seconds,
        api_user=config.remote_market_data_api_user,
        api_token=config.remote_market_data_api_token,
        verify_ssl=config.remote_market_data_verify_ssl,
    )


class RemoteFeedClient:
    """主项目对优美利SDK的窄接口适配器。"""

    def __init__(self, sdk_client):
        self.sdk_client = sdk_client

    def _ensure_direct_websocket_connection(self) -> None:
        """
        让WebSocket遵循SDK默认use_env_proxy=False的直连语义。

        外部SDK的REST会在use_env_proxy=False时绕过环境代理，但websockets库
        会独立读取HTTP_PROXY。把当前base_url主机追加到NO_PROXY，可避免局域网
        FeedHub被错误送往代理；不记录地址、用户名或Token。
        """

        if getattr(self.sdk_client, "use_env_proxy", False):
            return
        host = urlsplit(str(getattr(self.sdk_client, "base_url", ""))).hostname
        if not host:
            return
        existing_values = []
        for variable_name in ("NO_PROXY", "no_proxy"):
            existing_values.extend(
                item.strip()
                for item in os.environ.get(variable_name, "").split(",")
                if item.strip()
            )
        if host not in existing_values:
            existing_values.append(host)
        merged = ",".join(dict.fromkeys(existing_values))
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged

    def get_latest_ticks(
        self,
        codes: set[str] | frozenset[str] | list[str],
    ) -> dict[str, dict[str, Any] | None]:
        return self.sdk_client.get_latest_ticks(
            sorted(codes),
            expect_df=False,
        )

    def start_tick_callbacks(
        self,
        codes: set[str] | frozenset[str] | list[str],
        *,
        on_quote: Callable,
        on_subscribe: Callable,
        on_message: Callable,
        on_error: Callable,
    ):
        """只使用SDK 1.0公开的tick回调参数，不兼容旧channels/on_tick。"""

        self._ensure_direct_websocket_connection()
        return self.sdk_client.start_quote_callbacks(
            sorted(codes),
            freq="tick",
            on_quote=on_quote,
            on_subscribe=on_subscribe,
            on_message=on_message,
            on_error=on_error,
            daemon=False,
        )
