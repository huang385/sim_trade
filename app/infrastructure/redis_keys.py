"""集中维护 Redis 键名，避免各模块散落硬编码字符串。"""

from app.core.config import settings


# 已接受订单事件流。后续撮合服务将从该 Stream 消费订单事件。
ORDER_EVENT_STREAM = settings.order_stream_name
ORDER_EVENT_CONSUMER_GROUP = settings.order_consumer_group
ORDER_EVENT_DEAD_LETTER_STREAM = settings.order_dead_letter_stream

# 全部活动订单编号集合，用于重建对账，禁止使用KEYS扫描订单详情。
ACTIVE_ORDERS_ALL_KEY = "active_orders:all"

# 标准化行情事件流和优美利FeedHub连接状态Hash。
MARKET_TICK_STREAM = settings.market_tick_stream_name
YML_FEEDHUB_STATUS_KEY = "market:source:yml_feedhub:status"


def active_order_key(order_id: str) -> str:
    """返回单笔活动订单详情 Hash 的键名。"""

    return f"active_order:{order_id}"


def instrument_active_orders_key(exchange_id: str, symbol: str) -> str:
    """返回指定交易所、合约的活动订单 Set 键名。"""

    return f"active_orders:{exchange_id}:{symbol}"


def account_active_orders_key(account_id: str) -> str:
    """返回指定账户的活动订单 Set 键名。"""

    return f"account_active_orders:{account_id}"


def processed_order_event_key(event_id: str) -> str:
    """返回已处理订单事件的幂等标记键名。"""

    return f"processed_order_event:{event_id}"


def order_event_failure_key(message_id: str) -> str:
    """返回单条 Stream 消息失败次数的键名。"""

    return f"order_event_failure:{message_id}"


def market_latest_key(exchange_id: str, symbol: str) -> str:
    """返回指定交易所合约的最新标准化行情Hash键名。"""

    return f"market:latest:{exchange_id}:{symbol}"


def processed_market_tick_key(source_event_id: str) -> str:
    """返回行情源事件的Redis幂等标记键名。"""

    return f"processed_market_tick:{source_event_id}"
