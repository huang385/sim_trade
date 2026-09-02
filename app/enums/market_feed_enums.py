from enum import Enum

from app.enums.instrument_enums import InstrumentType


class MarketFeedDomain(str, Enum):
    """上游行情连接、订阅集合和事件流的稳定市场边界。"""

    FUTURES_MARKET = "FUTURES_MARKET"
    SECURITIES_MARKET = "SECURITIES_MARKET"


FUTURES_MARKET_INSTRUMENT_TYPES = frozenset(
    {
        InstrumentType.FUTURES.value,
        InstrumentType.FUTURES_OPTION.value,
        InstrumentType.INDEX.value,
        InstrumentType.INDEX_OPTION.value,
    }
)

SECURITIES_MARKET_INSTRUMENT_TYPES = frozenset(
    {
        InstrumentType.STOCK.value,
        InstrumentType.CONVERTIBLE_BOND.value,
    }
)


def resolve_market_feed_domain(instrument_type: object) -> MarketFeedDomain:
    """按服务端合约事实解析唯一行情域，未知产品禁止静默回退。"""

    value = getattr(instrument_type, "value", instrument_type)
    normalized = str(value or "").strip().upper()
    if normalized in FUTURES_MARKET_INSTRUMENT_TYPES:
        return MarketFeedDomain.FUTURES_MARKET
    if normalized in SECURITIES_MARKET_INSTRUMENT_TYPES:
        return MarketFeedDomain.SECURITIES_MARKET
    raise ValueError(f"合约类型未配置行情域: {normalized or '<empty>'}")
