from dataclasses import dataclass


@dataclass(frozen=True)
class MarketTickMatchResult:
    """一条Tick处理全部候选订单后的汇总，用于Worker日志和监控。"""

    # Redis合约活动订单Set提供的候选数量
    candidate_count: int
    # 纯撮合引擎判断可以成交的数量
    matched_count: int
    # 本轮新提交成交事务的数量
    settled_count: int
    # 重复投递时命中数据库幂等键的数量
    idempotent_count: int
    # 不成交、订单失效等无需重试的数量
    skipped_count: int
