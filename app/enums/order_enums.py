from enum import Enum


class OrderDirection(str, Enum):
    """
    订单买卖方向。

    BUY：
        买入。开仓时表示建立多头持仓。

    SELL：
        卖出。开仓时表示建立空头持仓。

    买卖方向不能单独判断订单是开仓还是平仓，
    必须结合 OffsetFlag 一起使用。
    """

    # 买入
    BUY = "BUY"

    # 卖出
    SELL = "SELL"


class OffsetFlag(str, Enum):
    """
    订单开平标志。

    第一阶段只开放 OPEN，其余枚举先为后续平仓链路预留。
    平仓功能需要持仓、持仓明细和冻结持仓等模块支持，
    不能直接复用当前的资金冻结流程。
    """

    # 开仓
    OPEN = "OPEN"

    # 普通平仓
    CLOSE = "CLOSE"

    # 平今仓
    CLOSE_TODAY = "CLOSE_TODAY"

    # 平昨仓
    CLOSE_YESTERDAY = "CLOSE_YESTERDAY"


class OrderType(str, Enum):
    """
    订单类型。

    第一阶段只实现限价单。市价单、条件单等类型需要新的
    价格保护和触发逻辑，后续再单独扩展。
    """

    # 限价单
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    """
    订单业务状态。

    当前阶段创建的合法订单直接进入 ACCEPTED，表示已经完成
    校验和资金冻结，正在等待后续撮合系统处理。
    """

    # 已收到，尚未完成业务校验
    NEW = "NEW"

    # 已校验并完成资源冻结，等待撮合
    ACCEPTED = "ACCEPTED"

    # 部分成交
    PARTIALLY_FILLED = "PARTIALLY_FILLED"

    # 部分成交后剩余数量已撤销，属于终态，不再等待撮合
    PARTIALLY_CANCELLED = "PARTIALLY_CANCELLED"

    # 全部成交
    FILLED = "FILLED"

    # 已撤销
    CANCELLED = "CANCELLED"

    # 已拒绝
    REJECTED = "REJECTED"


class OrderSubmitStatus(str, Enum):
    """
    订单提交处理状态。

    该状态描述订单接收流程本身是否完成，和成交状态分开保存。
    """

    # 正在提交和处理
    SUBMITTING = "SUBMITTING"

    # 提交已被系统接受
    ACCEPTED = "ACCEPTED"

    # 提交被系统拒绝
    REJECTED = "REJECTED"


class PositionDirection(str, Enum):
    """持仓方向；买入开仓形成多头，卖出开仓形成空头。"""

    LONG = "LONG"
    SHORT = "SHORT"


class PositionDetailStatus(str, Enum):
    """逐笔持仓明细状态。"""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class PositionFreezeAllocationStatus(str, Enum):
    """平仓订单逐笔持仓冻结分配状态。"""

    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


class OutboxStatus(str, Enum):
    """
    事务 Outbox 事件的处理状态。

    PENDING 表示事件已随业务事务可靠落库，等待发布；PROCESSING 表示
    某个 Worker 已领取该事件；SENT 表示已经写入 Redis Stream；FAILED
    表示重试次数耗尽，需要人工检查。状态存放在 PostgreSQL 中，因此
    Redis 临时不可用不会造成订单事件丢失。
    """

    # 等待发布，或者失败后等待下一次重试
    PENDING = "PENDING"

    # 已被一个发布 Worker 领取，正在写入 Redis
    PROCESSING = "PROCESSING"

    # 已经成功写入 Redis Stream
    SENT = "SENT"

    # 已达到最大重试次数，不再自动处理
    FAILED = "FAILED"
