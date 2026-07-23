from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class MatchableOrder:
    """
    撮合引擎需要的最小订单快照。

    这里只复制判断成交所需字段，不直接传入SQLAlchemy Order，确保
    VNMatchingEngine保持纯计算，也避免数据库Session关闭后访问延迟字段。
    """

    # 系统订单编号，用于把撮合结果交回结算层
    order_id: str
    # 买卖方向：BUY或SELL
    direction: str
    # 开平标志，本阶段只允许OPEN
    offset_flag: str
    # 订单类型，本阶段只允许LIMIT
    order_type: str
    # 用户提交的限价，和对手一价比较
    limit_price: Decimal
    # 生成快照时数据库中的剩余未成交数量
    remaining_volume: int


@dataclass(frozen=True)
class MatchResult:
    """
    一次订单与一条行情的纯撮合结果。

    该对象只描述计算结果，不代表数据库已经完成成交。只有后续
    TradeSettlementService事务成功提交后，订单和资金才真正发生变化。
    """

    # 是否满足限价和盘口量条件
    matched: bool
    # 参与撮合的订单编号
    order_id: str
    # 触发本次计算的行情源事件编号，也是成交幂等键的一部分
    market_event_id: str
    # 行情在Redis Stream中的消息编号
    market_stream_message_id: str
    # 实际成交价；不成交时为None
    fill_price: Decimal | None
    # 本次拟成交数量；不成交时为0
    fill_volume: int
    # 原始行情事件时间，成交落库时用作trade_time
    tick_event_time: datetime
    # 行情源序号，仅用于追踪，不用于过滤行情
    tick_sequence_id: int
    # 明确记录不成交原因，便于测试和日志定位
    reason: str | None = None


@dataclass(frozen=True)
class SettlementResult:
    """成交事务结果；幂等重放时返回原成交编号。"""

    # 新建或已经存在的成交编号；未成交、失效订单时为空
    trade_id: str | None
    # 本次处理的订单编号
    order_id: str
    # SETTLED、IDEMPOTENT、ORDER_INACTIVE等处理动作
    action: str


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
