from dataclasses import dataclass
from decimal import Decimal

from app.common.decimal_utils import quantize_money


@dataclass(frozen=True)
class AccountValuationResult:
    equity: Decimal
    available_cash: Decimal
    effective_required_margin: Decimal
    risk_available_cash: Decimal
    net_option_market_value: Decimal


class AccountValuationCalculator:
    """统一期货浮盈、期权净市值和风险保证金的纯账户汇总计算。"""

    @classmethod
    def calculate(
        cls,
        *,
        cash_balance: Decimal,
        futures_unrealized_pnl: Decimal,
        long_option_market_value: Decimal,
        short_option_market_value: Decimal,
        used_margin: Decimal,
        option_used_margin: Decimal,
        option_realtime_required_margin: Decimal,
        frozen_margin: Decimal,
        frozen_cash: Decimal,
        frozen_commission: Decimal,
        option_collateral_ratio: Decimal = Decimal("0"),
    ) -> AccountValuationResult:
        values = (
            cash_balance,
            futures_unrealized_pnl,
            long_option_market_value,
            short_option_market_value,
            used_margin,
            option_used_margin,
            option_realtime_required_margin,
            frozen_margin,
            frozen_cash,
            frozen_commission,
            option_collateral_ratio,
        )
        if any(not isinstance(value, Decimal) for value in values):
            raise TypeError("账户估值字段必须使用Decimal")
        net_option_value = quantize_money(
            long_option_market_value - short_option_market_value
        )
        equity = quantize_money(
            cash_balance + futures_unrealized_pnl + net_option_value
        )
        available = quantize_money(
            cash_balance
            + futures_unrealized_pnl
            + long_option_market_value * option_collateral_ratio
            - short_option_market_value
            - used_margin
            - frozen_margin
            - frozen_cash
            - frozen_commission
        )
        futures_margin = max(
            used_margin - option_used_margin,
            Decimal("0"),
        )
        effective_margin = quantize_money(
            futures_margin
            + max(option_used_margin, option_realtime_required_margin)
        )
        risk_available = quantize_money(
            cash_balance
            + futures_unrealized_pnl
            + long_option_market_value * option_collateral_ratio
            - short_option_market_value
            - effective_margin
            - frozen_margin
            - frozen_cash
            - frozen_commission
        )
        return AccountValuationResult(
            equity=equity,
            available_cash=available,
            effective_required_margin=effective_margin,
            risk_available_cash=risk_available,
            net_option_market_value=net_option_value,
        )

