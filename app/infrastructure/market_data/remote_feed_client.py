import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from app.core.config import Settings, settings


YMM_LIVE_DATA_SOURCE = "YMM_LIVE_DATA"
YMM_LIVE_DATA_SDK_DISTRIBUTION = "ymm-live-data-sdk"
YMM_LIVE_DATA_SDK_VERSION = "0.7.0"


class RemoteMarketDataConfigurationError(ValueError):
    """YMM Live Data启动配置不完整或不受支持。"""


class RemoteMarketDataSdkUnavailableError(RuntimeError):
    """运行环境尚未安装官方YMM Live Data客户端SDK。"""


def _normalized_mode(value: str) -> str | None:
    mode = value.strip()
    if not mode:
        return None
    normalized = {"lan": "lan", "ts": "TS", "local": "local"}.get(
        mode.lower()
    )
    if normalized is None:
        raise RemoteMarketDataConfigurationError(
            "REMOTE_MARKET_DATA_MODE必须是lan、TS或local"
        )
    return normalized


def remote_sdk_client_kwargs(config: Settings = settings) -> dict[str, Any]:
    """校验启动配置并返回官方构造器参数，不建立网络连接。"""

    token = config.remote_market_data_api_token.strip()
    mode = _normalized_mode(config.remote_market_data_mode)
    server_url = config.remote_market_data_base_url.strip() or None
    ca_file = config.remote_market_data_ca_file.strip() or None
    if not token:
        raise RemoteMarketDataConfigurationError(
            "缺少REMOTE_MARKET_DATA_API_TOKEN"
        )
    if mode is None and server_url is None:
        raise RemoteMarketDataConfigurationError(
            "REMOTE_MARKET_DATA_MODE和REMOTE_MARKET_DATA_BASE_URL至少配置一个"
        )
    if not config.remote_market_data_verify_ssl:
        raise RemoteMarketDataConfigurationError(
            "YMM Live Data客户端不允许关闭TLS证书校验"
        )

    return {
        "token": token,
        "mode": mode,
        "server_url": server_url,
        "ca_file": ca_file,
    }


def create_remote_sdk_client(
    config: Settings = settings,
    *,
    client_class=None,
):
    """使用公开构造器创建官方客户端，不记录地址、Token或会话信息。

    官方客户端在构造阶段立即建立WSS连接并完成Token认证。项目不调用
    包级全局init，避免测试或同进程其他组件共享隐式认证状态。
    """

    kwargs = remote_sdk_client_kwargs(config)
    if client_class is None:
        client_class = load_remote_sdk_client_class()

    return client_class(**kwargs)


def load_remote_sdk_client_class():
    """导入官方客户端类；缺少私有依赖时给出稳定且不含凭证的错误。"""

    try:
        from ymm_live_data_sdk import LiveMarketDataClient
    except ModuleNotFoundError as exc:
        raise RemoteMarketDataSdkUnavailableError(
            "未安装ymm-live-data-sdk==0.7.0；请从内部发布源安装客户端wheel"
        ) from exc
    return LiveMarketDataClient


def _stable_mapping(value: Any) -> dict[str, Any]:
    """把SDK公开对象复制成普通字典，禁止SDK类型进入业务线程。"""

    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        result = model_dump(mode="python")
        if isinstance(result, Mapping):
            return dict(result)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            records = to_dict(orient="records")
        except TypeError:
            records = to_dict()
        if isinstance(records, list) and len(records) == 1:
            records = records[0]
        if isinstance(records, Mapping):
            return dict(records)
    raise TypeError("YMM Live Data回调不是可支持的映射对象")


def _status_mapping(event: Any) -> dict[str, Any]:
    """只保留公开且不含凭证的状态字段。"""

    values = _stable_mapping(event)
    details = values.get("details")
    safe_details: dict[str, Any] = {}
    if isinstance(details, Mapping):
        for key in ("reason", "dropped_messages", "shard"):
            if key in details:
                safe_details[key] = details[key]
    return {
        "type": "status",
        "component": str(values.get("component") or "unknown"),
        "state": str(values.get("state") or "unknown"),
        "timestamp": values.get("timestamp"),
        "message": str(values.get("message") or ""),
        "details": safe_details,
        "sequence": values.get("sequence"),
    }


class YmmLiveDataSubscription:
    """包装一个官方客户端、一个行情消费者和一个状态消费者。"""

    def __init__(
        self,
        *,
        sdk_client,
        feed_thread: threading.Thread,
        status_thread: threading.Thread,
    ) -> None:
        self.sdk_client = sdk_client
        self.feed_thread = feed_thread
        self.status_thread = status_thread
        self._codes: frozenset[str] = frozenset()
        self._closed = False
        self._lock = threading.RLock()

    @staticmethod
    def _channels(codes) -> list[str]:
        return [f"tick_{code}" for code in sorted(set(codes))]

    def replace_codes(self, codes) -> dict[str, Any]:
        """使用SDK公开方法批量增订和退订，并生成统一订阅回执。"""

        desired = frozenset(str(code).strip().upper() for code in codes)
        with self._lock:
            if self._closed:
                raise RuntimeError("YMM Live Data客户端已经关闭")
            confirmed_before = {
                str(channel)[len("tick_") :].strip().upper()
                for channel in self.sdk_client.subscriptions
                if str(channel).startswith("tick_")
            }
            removed = confirmed_before - desired
            added = desired - confirmed_before
            if removed:
                self.sdk_client.unsubscribe(self._channels(removed))
            if added:
                self.sdk_client.subscribe(self._channels(added))
            self._codes = desired
            confirmed_channels = {
                str(channel)
                for channel in self.sdk_client.subscriptions
            }
        return {
            "contracts": {
                code: {
                    "exists": f"tick_{code}" in confirmed_channels,
                    "is_live": f"tick_{code}" in confirmed_channels,
                    "subscribed": f"tick_{code}" in confirmed_channels,
                }
                for code in sorted(desired)
            }
        }

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.sdk_client.close()

    def join(self, timeout: float | None = None) -> None:
        """等待两个SDK消费者退出；总等待时间不超过调用方给定值。"""

        if timeout is None:
            self.feed_thread.join()
            self.status_thread.join()
            return
        half = max(timeout, 0) / 2
        self.feed_thread.join(timeout=half)
        self.status_thread.join(timeout=half)

    def is_alive(self) -> bool:
        with self._lock:
            if self._closed:
                return False
        return self.feed_thread.is_alive() and self.status_thread.is_alive()


class RemoteFeedClient:
    """主项目对YMM Live Data官方SDK的窄接口适配器。"""

    def __init__(self, sdk_client_or_factory):
        if callable(sdk_client_or_factory) and not hasattr(
            sdk_client_or_factory,
            "listen",
        ):
            self._sdk_factory = sdk_client_or_factory
            self.sdk_client = None
        else:
            self._sdk_factory = lambda: sdk_client_or_factory
            self.sdk_client = sdk_client_or_factory
        self._subscription: YmmLiveDataSubscription | None = None

    def start_tick_callbacks(
        self,
        codes: set[str] | frozenset[str] | list[str],
        *,
        on_quote: Callable,
        on_subscribe: Callable,
        on_message: Callable,
        on_error: Callable,
    ) -> YmmLiveDataSubscription:
        """先启动状态/行情消费者，再一次性提交当前Tick频道。"""

        if self._subscription is not None and self._subscription.is_alive():
            raise RuntimeError("同一个客户端只能启动一个行情消费者")
        self.sdk_client = self._sdk_factory()

        def handle_tick(messages: Any) -> None:
            """兼容 SDK 0.6 的单条回调和 0.7 的批量回调。"""

            batch = messages if isinstance(messages, tuple) else (messages,)
            for message in batch:
                try:
                    data = _stable_mapping(message)
                    raw = {
                        "action": data.get("action"),
                        "channel": data.get("channel"),
                    }
                    on_quote(data, raw)
                except Exception as exc:
                    # 单条坏行情不能影响同批其他行情，更不能杀死 SDK
                    # 行情线程；只向 Worker 传递脱敏错误类型。
                    on_error({"raw": {"code": type(exc).__name__}})

        def handle_status(event: Any) -> None:
            try:
                message = _status_mapping(event)
                on_message(message)
                if message["state"] in {
                    "disconnected",
                    "reconnecting",
                    "partial",
                    "quota_exceeded",
                    "degraded",
                    "slow_consumer",
                    "error",
                    "replaced",
                } and not (
                    message["component"] == "storage"
                    and message["state"] == "slow_consumer"
                ):
                    # storage/slow_consumer 只描述行情中心历史存储积压，
                    # 已经由状态回调交给Worker处理；不要再作为运行错误重复
                    # 转发。会话级slow_consumer仍必须进入on_error。
                    code = (
                        f"{message['component']}_{message['state']}"
                        .upper()
                    )
                    on_error({"raw": {"code": code}})
            except Exception as exc:
                on_error({"raw": {"code": type(exc).__name__}})

        # 文档要求先消费状态，再消费行情，最后订阅，防止早期事件无人读取。
        status_thread = self.sdk_client.listen_status(handle_status)
        feed_thread = self.sdk_client.listen(handle_tick)
        subscription = YmmLiveDataSubscription(
            sdk_client=self.sdk_client,
            feed_thread=feed_thread,
            status_thread=status_thread,
        )
        try:
            report = subscription.replace_codes(codes)
        except Exception:
            subscription.stop()
            subscription.join(timeout=5)
            raise
        self._subscription = subscription
        on_subscribe(report)
        return subscription

    def replace_tick_subscriptions(
        self,
        codes: set[str] | frozenset[str] | list[str],
    ) -> dict[str, Any]:
        if self._subscription is None:
            raise RuntimeError("行情消费者尚未启动")
        return self._subscription.replace_codes(codes)

    def close(self) -> None:
        if self._subscription is not None:
            self._subscription.stop()
        else:
            self.sdk_client.close()
