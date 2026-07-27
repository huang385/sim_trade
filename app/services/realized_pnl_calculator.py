from decimal import Decimal

from app.common.decimal_utils import quantize_money
from app.common.exceptions import BusinessValidationError
from app.enums.order_enums import OrderDirection


class RealizedPnlCalculator:
    """使用逐笔开仓价计算平仓已实现盈亏的纯计算器。"""

    @staticmethod
    def calculate(
        *,
        close_direction: str,
        open_price: Decimal,
        close_price: Decimal,
        volume: int,
        contract_multiplier: Decimal,
    ) -> Decimal:
        if close_direction == OrderDirection.SELL.value:
            price_difference = close_price - open_price
        elif close_direction == OrderDirection.BUY.value:
            price_difference = open_price - close_price
        else:
            raise BusinessValidationError(
                "平仓买卖方向不合法",
                error_code="INVALID_CLOSE_DIRECTION",
            )
        return quantize_money(
            price_difference * Decimal(volume) * contract_multiplier
        )
