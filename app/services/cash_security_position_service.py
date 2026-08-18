"""Cash-security position mutations with explicit settlement availability rules."""

from decimal import Decimal

from app.common.exceptions import DataAccessError
from app.common.decimal_utils import quantize_money


class CashSecurityPositionService:
    """Owns STOCK T+1 and CONVERTIBLE_BOND same-day availability semantics."""

    @staticmethod
    def apply_buy(position, *, instrument_type: str, volume: int, turnover: Decimal) -> None:
        position.total_volume += volume
        position.today_volume += volume
        if instrument_type == "STOCK":
            position.settlement_locked_volume += volume
        elif instrument_type == "CONVERTIBLE_BOND":
            position.available_volume += volume
        else:
            raise DataAccessError(
                "现金证券持仓产品类型无效",
                error_code="CASH_SECURITY_POSITION_INSTRUMENT_INVALID",
            )
        position.position_cost = quantize_money(position.position_cost + turnover)
        # A same-day purchase contributes from its actual fill price, while
        # inventory carried overnight keeps the EOD mark as its daily basis.
        position.daily_pnl_base_cost = quantize_money(
            Decimal(getattr(position, "daily_pnl_base_cost", Decimal("0"))) + turnover
        )
        position.average_open_price = quantize_money(
            position.position_cost / Decimal(position.total_volume)
        )

    @staticmethod
    def apply_sell(position, *, instrument_type: str, volume: int) -> Decimal:
        if position.frozen_volume < volume or position.total_volume < volume:
            raise DataAccessError(
                "现金证券卖出冻结事实不一致",
                error_code="CASH_SECURITY_SELL_FREEZE_INCONSISTENT",
            )
        if instrument_type == "STOCK":
            # T+1 stock orders can only freeze yesterday's available inventory.
            if position.yesterday_volume < volume:
                raise DataAccessError(
                    "股票卖出不能消耗当日锁定持仓",
                    error_code="STOCK_T_PLUS_ONE_VIOLATION",
                )
            position.yesterday_volume -= volume
        elif instrument_type == "CONVERTIBLE_BOND":
            # Convertible bonds may use available inventory from either bucket.
            from_yesterday = min(position.yesterday_volume, volume)
            position.yesterday_volume -= from_yesterday
            position.today_volume -= volume - from_yesterday
        else:
            raise DataAccessError(
                "现金证券持仓产品类型无效",
                error_code="CASH_SECURITY_POSITION_INSTRUMENT_INVALID",
            )
        cost = (
            position.position_cost
            if volume == position.total_volume
            else quantize_money(position.position_cost * Decimal(volume) / Decimal(position.total_volume))
        )
        daily_base = Decimal(getattr(position, "daily_pnl_base_cost", Decimal("0")))
        daily_base_reduction = (
            daily_base
            if volume == position.total_volume
            else quantize_money(daily_base * Decimal(volume) / Decimal(position.total_volume))
        )
        position.frozen_volume -= volume
        position.total_volume -= volume
        position.position_cost = quantize_money(position.position_cost - cost)
        position.daily_pnl_base_cost = quantize_money(
            daily_base - daily_base_reduction
        )
        position.available_volume = (
            position.total_volume
            - position.frozen_volume
            - position.settlement_locked_volume
        )
        position.average_open_price = (
            quantize_money(position.position_cost / Decimal(position.total_volume))
            if position.total_volume
            else Decimal("0")
        )
        return cost
