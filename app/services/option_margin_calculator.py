from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.enums.option_enums import MarginPriceMode, OptionType


@dataclass(frozen=True)
class OptionMarginRuleSnapshot:
    """下单或估值周期使用的不可变期权保证金规则。"""

    rule_id: int
    rule_version: str
    margin_algorithm: str
    margin_adjustment_rate: Decimal
    minimum_guarantee_rate: Decimal
    out_of_money_deduction_rate: Decimal
    minimum_underlying_margin_ratio: Decimal
    extra_margin_rate: Decimal

    def to_json_mapping(self) -> dict[str, str | int]:
        """Decimal以字符串保存，避免JSON快照引入二进制浮点误差。"""

        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "margin_algorithm": self.margin_algorithm,
            "margin_adjustment_rate": format(
                self.margin_adjustment_rate, "f"
            ),
            "minimum_guarantee_rate": format(
                self.minimum_guarantee_rate, "f"
            ),
            "out_of_money_deduction_rate": format(
                self.out_of_money_deduction_rate, "f"
            ),
            "minimum_underlying_margin_ratio": format(
                self.minimum_underlying_margin_ratio, "f"
            ),
            "extra_margin_rate": format(self.extra_margin_rate, "f"),
        }


@dataclass(frozen=True)
class OptionMarginInput:
    """纯保证金计算所需的全部标量输入。"""

    option_type: OptionType
    strike_price: Decimal
    option_price: Decimal
    underlying_price: Decimal
    option_multiplier: Decimal
    underlying_multiplier: Decimal
    volume: int
    price_mode: MarginPriceMode
    calculated_at: datetime
    rule: OptionMarginRuleSnapshot
    underlying_margin_per_lot: Decimal = Decimal("0")


@dataclass(frozen=True)
class OptionMarginResult:
    """可审计的期权保证金计算结果。"""

    margin_per_lot: Decimal
    total_margin: Decimal
    premium_component: Decimal
    risk_component: Decimal
    out_of_money_amount: Decimal
    underlying_price: Decimal
    option_price: Decimal
    rule_id: int
    rule_version: str
    price_mode: MarginPriceMode
    calculated_at: datetime
    calculation_version: str = "OPTION_MARGIN_V1"


class OptionMarginCalculator(Protocol):
    """期权保证金计算器协议。"""

    def calculate(self, request: OptionMarginInput) -> OptionMarginResult:
        ...

