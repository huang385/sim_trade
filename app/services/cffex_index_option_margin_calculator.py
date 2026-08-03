from decimal import Decimal

from app.common.decimal_utils import quantize_money
from app.common.exceptions import BusinessValidationError
from app.enums.option_enums import OptionType
from app.services.option_margin_calculator import (
    OptionMarginInput,
    OptionMarginResult,
)


class CffexIndexOptionMarginCalculator:
    """
    中金所股指期权卖方保证金纯计算器。

    本阶段只提供单元测试和未来扩展能力，不接入股指期权卖出开仓。
    """

    def calculate(self, request: OptionMarginInput) -> OptionMarginResult:
        values = (
            request.strike_price,
            request.option_price,
            request.underlying_price,
            request.option_multiplier,
        )
        if any(not isinstance(value, Decimal) for value in values):
            raise BusinessValidationError(
                "股指期权保证金输入必须使用Decimal",
                error_code="INVALID_OPTION_MARGIN_TYPE",
            )
        if (
            request.strike_price <= 0
            or request.option_price < 0
            or request.underlying_price <= 0
            or request.option_multiplier <= 0
            or request.volume <= 0
        ):
            raise BusinessValidationError(
                "股指期权保证金输入不合法",
                error_code="INVALID_OPTION_MARGIN_INPUT",
            )

        rule = request.rule
        if request.option_type == OptionType.CALL:
            out_of_money_points = max(
                request.strike_price - request.underlying_price,
                Decimal("0"),
            )
            minimum_component = (
                rule.minimum_guarantee_rate
                * request.underlying_price
                * request.option_multiplier
                * rule.margin_adjustment_rate
            )
        else:
            out_of_money_points = max(
                request.underlying_price - request.strike_price,
                Decimal("0"),
            )
            minimum_component = (
                rule.minimum_guarantee_rate
                * request.strike_price
                * request.option_multiplier
                * rule.margin_adjustment_rate
            )
        out_of_money = quantize_money(
            out_of_money_points * request.option_multiplier
        )
        premium = quantize_money(
            request.option_price * request.option_multiplier
        )
        adjusted_underlying = (
            request.underlying_price
            * request.option_multiplier
            * rule.margin_adjustment_rate
        )
        risk = quantize_money(
            max(adjusted_underlying - out_of_money, minimum_component)
        )
        margin_per_lot = quantize_money(
            (premium + risk) * (Decimal("1") + rule.extra_margin_rate)
        )
        return OptionMarginResult(
            margin_per_lot=margin_per_lot,
            total_margin=quantize_money(
                margin_per_lot * Decimal(request.volume)
            ),
            premium_component=premium,
            risk_component=risk,
            out_of_money_amount=out_of_money,
            underlying_price=request.underlying_price,
            option_price=request.option_price,
            rule_id=rule.rule_id,
            rule_version=rule.rule_version,
            price_mode=request.price_mode,
            calculated_at=request.calculated_at,
        )

