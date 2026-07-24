from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.enums.order_enums import OffsetFlag, OrderDirection, OrderType


@dataclass(frozen=True)
class MatchingOrder:
    """
    撮合引擎需要的最小订单快照。

    快照与 SQLAlchemy Order 完全解耦，数据库 Session 关闭后仍可安全使用。
    frozen=True 保证纯撮合引擎不能意外修改订单状态或剩余数量。
    """

    # 系统订单编号，用于把撮合结果交回成交结算层。
    order_id: str
    # 买卖方向；核心引擎据此选择对手方一档盘口。
    direction: OrderDirection
    # 开平标志；本阶段由业务编排层限制为 OPEN，核心算法不据此拒绝。
    offset_flag: OffsetFlag
    # 订单类型；本阶段由业务编排层限制为 LIMIT，核心算法不据此拒绝。
    order_type: OrderType
    # 用户委托限价，必须始终使用 Decimal。
    limit_price: Decimal
    # 生成快照时数据库中的剩余未成交数量。
    remaining_volume: int


@dataclass(frozen=True)
class MatchingMarketData:
    """
    纯撮合使用的一档行情快照。

    event_time 和 sequence_id 当前只用于成交追踪，不承担行情新旧或重复过滤。
    事件编号和 Stream 消息编号随结果返回，供结算层建立成交幂等关系。
    """

    # 行情源事件编号。
    event_id: str
    # 行情在 Redis Stream 中的消息编号。
    stream_message_id: str
    # 买一价和买一量；没有有效买一价时价格为 None。
    bid_price_1: Decimal | None
    bid_volume_1: int
    # 卖一价和卖一量；没有有效卖一价时价格为 None。
    ask_price_1: Decimal | None
    ask_volume_1: int
    # 行情源事件时间和序号，仅用于追踪及后续扩展。
    event_time: datetime
    sequence_id: int


@dataclass(frozen=True)
class MatchResult:
    """
    纯撮合计算结果，不代表数据库已经生成 Trade。

    只有 TradeSettlementService 在数据库事务中成功提交后，订单、资金和
    持仓才真正发生变化。engine_name/version 用于确认结果来自哪套算法。
    """

    # 是否满足成交条件。
    matched: bool
    # 参与计算的订单编号。
    order_id: str
    # 行情事件和 Stream 消息编号，供结算幂等及问题追踪使用。
    market_event_id: str
    market_stream_message_id: str
    # 拟成交价格和数量；未成交时分别为 None 和 0。
    fill_price: Decimal | None
    fill_volume: int
    # 原始行情时间和序号，落库及追踪继续沿用现有语义。
    tick_event_time: datetime
    tick_sequence_id: int
    # 未成交或拒绝原因；正常成交时为 None。
    reason: str | None
    # 产生结果的撮合引擎标识。
    engine_name: str
    engine_version: str
