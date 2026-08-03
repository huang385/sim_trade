from decimal import Decimal

from app.common.decimal_utils import quantize_money
from app.common.exceptions import BusinessValidationError
from app.enums.option_enums import OptionType
from app.services.option_margin_calculator import (
    OptionMarginInput,
    OptionMarginResult,
)


class CommodityFuturesOptionMarginCalculator:
    """按配置参数计算商品期权卖方逐手保证金。"""

    @staticmethod
    def _validate(request: OptionMarginInput) -> None:
        decimal_fields = (
            request.strike_price,
            request.option_price,
            request.underlying_price,
            request.option_multiplier,
            request.underlying_multiplier,
            request.underlying_margin_per_lot,
        )
        if any(not isinstance(value, Decimal) for value in decimal_fields):
            raise BusinessValidationError(
                "期权保证金输入必须使用Decimal",
                error_code="INVALID_OPTION_MARGIN_TYPE",
            )
        if (
            request.strike_price <= 0
            or request.option_price < 0
            or request.underlying_price <= 0
            or request.option_multiplier <= 0
            or request.underlying_multiplier <= 0
            or request.underlying_margin_per_lot < 0
            or request.volume <= 0
        ):
            raise BusinessValidationError(
                "期权保证金输入不合法",
                error_code="INVALID_OPTION_MARGIN_INPUT",
            )

    def calculate(self, request: OptionMarginInput) -> OptionMarginResult:
        self._validate(request)
        if request.option_type == OptionType.CALL:
            out_of_money_points = max(
                request.strike_price - request.underlying_price,
                Decimal("0"),
            )
        else:
            out_of_money_points = max(
                request.underlying_price - request.strike_price,
                Decimal("0"),
            )
        out_of_money = quantize_money(
            out_of_money_points * request.underlying_multiplier
        )
        premium = quantize_money(
            request.option_price * request.option_multiplier
        )
        rule = request.rule
        risk = max(
            request.underlying_margin_per_lot
            - out_of_money * rule.out_of_money_deduction_rate,
            request.underlying_margin_per_lot
            * rule.minimum_underlying_margin_ratio,
        )
        risk = quantize_money(max(risk, Decimal("0")))
        margin_per_lot = quantize_money(
            (premium + risk) * (Decimal("1") + rule.extra_margin_rate)
        )
        total = quantize_money(
            margin_per_lot * Decimal(request.volume)
        )
        return OptionMarginResult(
            margin_per_lot=margin_per_lot,
            total_margin=total,
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

