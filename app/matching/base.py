from typing import Protocol, runtime_checkable

from app.matching.models import MatchResult, MatchingMarketData, MatchingOrder


@runtime_checkable
class MatchingEngine(Protocol):
    """
    所有撮合引擎必须遵守的纯计算接口。

    上层编排服务只依赖本接口，不感知具体使用 VN 或未来其他引擎。
    引擎实例应当无状态并可重复使用，不能在每条 Tick 到达时重新创建。
    """

    # 用于配置选择、日志和成交结果追踪的稳定标识。
    name: str
    version: str

    def match(
        self,
        order: MatchingOrder,
        market: MatchingMarketData,
    ) -> MatchResult:
        """使用订单和行情的不可变快照计算一次拟成交结果。"""

        ...
