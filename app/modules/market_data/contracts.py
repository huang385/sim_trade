from collections.abc import Callable, Iterable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MarketDataSubscription(Protocol):
    """行情订阅生命周期的最小端口。"""

    def replace_codes(self, codes: Iterable[str]) -> dict[str, Any]: ...

    def stop(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...


@runtime_checkable
class MarketDataProvider(Protocol):
    """行情模块定义、基础设施实现的公司行情源端口。"""

    def start_tick_callbacks(
        self,
        codes: Iterable[str],
        *,
        on_quote: Callable,
        on_subscribe: Callable,
        on_message: Callable,
        on_error: Callable,
    ) -> MarketDataSubscription: ...

    def replace_tick_subscriptions(
        self,
        codes: Iterable[str],
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...
