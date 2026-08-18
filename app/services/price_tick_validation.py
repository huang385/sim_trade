"""产品无关的 Decimal 价格档位校验。"""

from decimal import Decimal

from app.common.exceptions import BusinessValidationError


def validate_price_tick(*, price: Decimal, price_tick: Decimal) -> None:
    if price_tick <= Decimal("0"):
        raise BusinessValidationError(
            "最小变动价位不合法", error_code="INVALID_PRICE_TICK"
        )
    if price % price_tick != Decimal("0"):
        raise BusinessValidationError(
            f"委托价格不符合最小变动价位 {price_tick}",
            error_code="PRICE_TICK_MISMATCH",
        )
