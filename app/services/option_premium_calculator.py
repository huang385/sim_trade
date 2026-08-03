from decimal import Decimal

from app.common.decimal_utils import quantize_money
from app.common.exceptions import BusinessValidationError


class OptionPremiumCalculator:
    """期权权利金纯计算器。"""

    @staticmethod
    def calculate(
        *,
        price: Decimal,
        volume: int,
        multiplier: Decimal,
    ) -> Decimal:
        if not isinstance(price, Decimal) or not isinstance(
            multiplier, Decimal
        ):
            raise BusinessValidationError(
                "期权价格和乘数必须使用Decimal",
                error_code="INVALID_OPTION_PREMIUM_TYPE",
            )
        if price <= 0 or multiplier <= 0 or volume <= 0:
            raise BusinessValidationError(
                "期权权利金参数不合法",
                error_code="INVALID_OPTION_PREMIUM_INPUT",
            )
        return quantize_money(price * Decimal(volume) * multiplier)

