"""集中维护 Redis 键名，避免各模块散落硬编码字符串。"""

from app.core.config import settings


ORDER_EVENT_STREAM = settings.order_stream_name
ORDER_EVENT_CONSUMER_GROUP = settings.order_consumer_group
ORDER_EVENT_DEAD_LETTER_STREAM = settings.order_dead_letter_stream

# 全部活动订单编号集合，用于重建对账，禁止使用 KEYS 扫描订单详情。
ACTIVE_ORDERS_ALL_KEY = "active_orders:all"

MARKET_TICK_STREAM = settings.market_tick_stream_name
MARKET_MATCHING_CONSUMER_GROUP = settings.market_matching_consumer_group
MARKET_MATCHING_DEAD_LETTER_STREAM = settings.market_matching_dead_letter_stream
PNL_CONSUMER_GROUP = settings.pnl_consumer_group
PNL_DEAD_LETTER_STREAM = settings.pnl_dead_letter_stream
PNL_DIRTY_POSITIONS_KEY = "pnl:dirty_positions"
PNL_DIRTY_ACCOUNTS_KEY = "pnl:dirty_accounts"
PNL_DIRTY_POSITION_VERSIONS_KEY = "pnl:dirty_position_versions"
PNL_POSITION_CACHE_VERSION_KEY = "pnl:position_cache_version"
PNL_DIRTY_CONTRACTS_KEY = "pnl:dirty_contracts"
PNL_DIRTY_CONTRACT_VERSIONS_KEY = "pnl:dirty_contract_versions"
PNL_ACCOUNT_INDEX_KEYS_KEY = "pnl:index_keys:accounts"
PNL_CONTRACT_INDEX_KEYS_KEY = "pnl:index_keys:contracts"
PNL_WORKER_LEASE_KEY = "pnl:worker:lease"
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
    """返回单条订单 Stream 消息失败次数的键名。"""

    return f"order_event_failure:{message_id}"


def market_latest_key(exchange_id: str, symbol: str) -> str:
    """返回指定交易所、合约的最新标准化行情 Hash 键名。"""

    return f"market:latest:{exchange_id}:{symbol}"


def market_matching_failure_key(message_id: str) -> str:
    """返回行情撮合消息的失败次数键名。"""

    return f"market_matching_failure:{message_id}"


def pnl_position_key(position_id: str) -> str:
    """返回单条持仓实时盈亏Hash键名。"""

    return f"pnl:position:{position_id}"


def pnl_account_key(account_id: str) -> str:
    """返回账户实时盈亏Hash键名。"""

    return f"pnl:account:{account_id}"


def pnl_account_positions_key(account_id: str) -> str:
    """返回账户当前已建立实时快照的持仓编号集合。"""

    return f"pnl:account_positions:{account_id}"


def pnl_contract_positions_key(exchange_id: str, symbol: str) -> str:
    """返回合约当前已建立实时快照的持仓编号集合。"""

    return f"pnl:contract_positions:{exchange_id}:{symbol}"


def pnl_dirty_contract_member(exchange_id: str, symbol: str) -> str:
    """返回Dirty合约集合成员，百分号编码避免分隔符歧义。"""

    from urllib.parse import quote

    return (
        f"{quote(exchange_id.strip().upper(), safe='')}"
        f"|{quote(symbol.strip().upper(), safe='')}"
    )


def parse_pnl_dirty_contract_member(member: str) -> tuple[str, str]:
    """把Dirty合约成员还原为标准交易所和合约代码。"""

    from urllib.parse import unquote

    exchange_id, symbol = member.split("|", 1)
    return unquote(exchange_id), unquote(symbol)


def pnl_dirty_contract_accounts_key(
    exchange_id: str,
    symbol: str,
) -> str:
    """返回成交Dirty合约关联账户集合。"""

    member = pnl_dirty_contract_member(exchange_id, symbol)
    return f"pnl:dirty_contract_accounts:{member}"


def pnl_event_failure_key(message_id: str) -> str:
    """返回PnL行情消费失败次数键名。"""

    return f"pnl_event_failure:{message_id}"


def pnl_trade_event_failure_key(message_id: str) -> str:
    """返回成交后PnL刷新消费者的失败次数键名。"""

    return f"pnl_trade_event_failure:{message_id}"
