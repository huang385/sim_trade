"""Pure best-opposite matching for stock and convertible-bond cash orders."""

from dataclasses import dataclass
from decimal import Decimal

from app.enums.order_enums import OrderDirection


@dataclass(frozen=True)
class CashSecurityOrderSnapshot:
    order_id: str
    instrument_type: str
    direction: str
    limit_price: Decimal
    remaining_volume: int


@dataclass(frozen=True)
class CashSecurityMarketSnapshot:
    bid_price_1: Decimal | None
    bid_volume_1: int
    ask_price_1: Decimal | None
    ask_volume_1: int


@dataclass(frozen=True)
class CashSecurityMatchResult:
    matched: bool
    fill_price: Decimal | None
    fill_volume: int
    reason: str | None = None


class CashSecurityMatchingStrategy:
    """Match only against the valid best opposite quote, without settlement."""

    supported_instrument_types = frozenset({"STOCK", "CONVERTIBLE_BOND"})

    def match(self, order: CashSecurityOrderSnapshot, market: CashSecurityMarketSnapshot) -> CashSecurityMatchResult:
        if order.instrument_type not in self.supported_instrument_types:
            return CashSecurityMatchResult(False, None, 0, "UNSUPPORTED_INSTRUMENT")
        if order.remaining_volume <= 0:
            return CashSecurityMatchResult(False, None, 0, "NO_REMAINING_VOLUME")
        if order.direction == OrderDirection.BUY.value:
            price, volume = market.ask_price_1, market.ask_volume_1
            if price is None or price <= 0:
                return CashSecurityMatchResult(False, None, 0, "INVALID_ASK_PRICE")
            if volume <= 0:
                return CashSecurityMatchResult(False, None, 0, "NO_ASK_VOLUME")
            if order.limit_price < price:
                return CashSecurityMatchResult(False, None, 0, "BUY_LIMIT_NOT_REACHED")
        elif order.direction == OrderDirection.SELL.value:
            price, volume = market.bid_price_1, market.bid_volume_1
            if price is None or price <= 0:
                return CashSecurityMatchResult(False, None, 0, "INVALID_BID_PRICE")
            if volume <= 0:
                return CashSecurityMatchResult(False, None, 0, "NO_BID_VOLUME")
            if order.limit_price > price:
                return CashSecurityMatchResult(False, None, 0, "SELL_LIMIT_NOT_REACHED")
        else:
            return CashSecurityMatchResult(False, None, 0, "UNSUPPORTED_DIRECTION")
        return CashSecurityMatchResult(True, price, min(order.remaining_volume, volume))
