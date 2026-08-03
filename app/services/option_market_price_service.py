from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.common.exceptions import BusinessRuleError
from app.infrastructure.market_data.market_tick_store import MarketTickStore


@dataclass(frozen=True)
class OptionMarginMarketPrices:
    """期权卖方保证金冻结采用的有效行情价格。"""

    option_price: Decimal
    underlying_price: Decimal


class OptionMarketPriceService:
    """从Redis最新行情读取期权及标的价格，不修改任何资金事实。"""

    def __init__(self, market_tick_store: MarketTickStore):
        self.market_tick_store = market_tick_store

    @staticmethod
    def _last_price(values: dict[str, str], *, code: str) -> Decimal:
        try:
            price = Decimal(values.get("last_price", ""))
        except (InvalidOperation, ValueError) as exc:
            raise BusinessRuleError(
                f"{code}缺少有效行情价格",
                error_code="OPTION_MARKET_PRICE_UNAVAILABLE",
            ) from exc
        if not price.is_finite() or price <= 0:
            raise BusinessRuleError(
                f"{code}缺少有效行情价格",
                error_code="OPTION_MARKET_PRICE_UNAVAILABLE",
            )
        return price

    def get_margin_prices(
        self,
        *,
        option_instrument,
        underlying_instrument,
        order_limit_price: Decimal,
    ) -> OptionMarginMarketPrices:
        snapshots = self.market_tick_store.get_latest_many(
            {
                (
                    option_instrument.exchange_id,
                    option_instrument.symbol,
                ),
                (
                    underlying_instrument.exchange_id,
                    underlying_instrument.symbol,
                ),
            }
        )
        option_latest = self._last_price(
            snapshots.get(
                (
                    option_instrument.exchange_id,
                    option_instrument.symbol,
                ),
                {},
            ),
            code=option_instrument.symbol,
        )
        underlying_price = self._last_price(
            snapshots.get(
                (
                    underlying_instrument.exchange_id,
                    underlying_instrument.symbol,
                ),
                {},
            ),
            code=underlying_instrument.symbol,
        )
        # 两者都是期权价格，可以安全采用更保守的较高值。标的价格不与
        # 期权价格直接比较，避免混淆不同含义和单位。
        return OptionMarginMarketPrices(
            option_price=max(order_limit_price, option_latest),
            underlying_price=underlying_price,
        )

