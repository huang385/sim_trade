"""Cash-security position mutations with explicit settlement availability rules."""

from decimal import Decimal

from app.common.exceptions import DataAccessError
from app.common.decimal_utils import quantize_money


class CashSecurityPositionService:
    """Owns per-product cash-security sell-availability semantics."""

    @staticmethod
    def settlement_days(*, instrument_type: str, market_tplus: int | None = None) -> int:
        if instrument_type == "STOCK":
            return 1
        if instrument_type == "CONVERTIBLE_BOND":
            return 0
        if instrument_type == "ETF" and market_tplus in {0, 1}:
            return int(market_tplus)
        raise DataAccessError(
            "现金证券持仓产品类型或T+属性无效",
            error_code="CASH_SECURITY_POSITION_INSTRUMENT_INVALID",
        )

    @staticmethod
    def apply_buy(
        position,
        *,
        instrument_type: str,
        volume: int,
        turnover: Decimal,
        market_tplus: int | None = None,
    ) -> None:
        position.total_volume += volume
        position.today_volume += volume
        if CashSecurityPositionService.settlement_days(
            instrument_type=instrument_type, market_tplus=market_tplus
        ) == 1:
            position.settlement_locked_volume += volume
        else:
            position.available_volume += volume
        position.position_cost = quantize_money(position.position_cost + turnover)
        # 0028 installed non-null zero buckets for historical rows.  A false
        # flag means those zeros are *unknown*, not an established allocation.
        # Keep the old aggregate basis authoritative until EOD can establish a
        # verified yesterday bucket; only the newly bought portion is known.
        if not getattr(position, "daily_pnl_base_established", True):
            position.today_pnl_base_cost = quantize_money(
                Decimal(getattr(position, "today_pnl_base_cost", Decimal("0")))
                + turnover
            )
            position.daily_pnl_base_cost = quantize_money(
                Decimal(getattr(position, "daily_pnl_base_cost", Decimal("0"))) + turnover
            )
            position.average_open_price = quantize_money(
                position.position_cost / Decimal(position.total_volume)
            )
            return
        # 当日买入部分按实际成交额计入基准；隔夜持仓则保留昨日收盘盯市值，
        # 两者相加后才能正确计算当日持仓盈亏。
        today_base = Decimal(getattr(position, "today_pnl_base_cost", Decimal("0")))
        # Compatibility for rows/tests created before the explicit buckets:
        # their aggregate base represented the carried (yesterday) bucket.
        prior_daily_base = Decimal(
            getattr(position, "daily_pnl_base_cost", Decimal("0"))
        )
        prior_yesterday_base = getattr(position, "yesterday_pnl_base_cost", None)
        yesterday_base = Decimal(
            prior_daily_base - today_base
            if prior_yesterday_base is None
            else prior_yesterday_base
        )
        position.yesterday_pnl_base_cost = yesterday_base
        position.today_pnl_base_cost = quantize_money(today_base + turnover)
        position.daily_pnl_base_cost = quantize_money(
            position.yesterday_pnl_base_cost
            + Decimal(position.today_pnl_base_cost)
        )
        position.daily_pnl_base_established = True
        position.average_open_price = quantize_money(
            position.position_cost / Decimal(position.total_volume)
        )

    @staticmethod
    def apply_sell(
        position,
        *,
        instrument_type: str,
        volume: int,
        market_tplus: int | None = None,
    ) -> Decimal:
        if position.frozen_volume < volume or position.total_volume < volume:
            raise DataAccessError(
                "现金证券卖出冻结事实不一致",
                error_code="CASH_SECURITY_SELL_FREEZE_INCONSISTENT",
            )
        total_before = position.total_volume
        yesterday_before = position.yesterday_volume
        today_before = position.today_volume
        prior_yesterday_base = getattr(position, "yesterday_pnl_base_cost", None)
        yesterday_base = Decimal(
            getattr(position, "daily_pnl_base_cost", Decimal("0"))
            if prior_yesterday_base is None
            else prior_yesterday_base
        )
        today_base = Decimal(
            getattr(position, "today_pnl_base_cost", Decimal("0"))
        )
        settlement_days = CashSecurityPositionService.settlement_days(
            instrument_type=instrument_type, market_tplus=market_tplus
        )
        if settlement_days == 1:
            # T+1证券只能卖出昨仓，不能使用当日买入数量。
            if position.yesterday_volume < volume:
                raise DataAccessError(
                    "T+1现金证券卖出不能消耗当日锁定持仓",
                    error_code=(
                        "STOCK_T_PLUS_ONE_VIOLATION"
                        if instrument_type == "STOCK"
                        else "ETF_T_PLUS_ONE_VIOLATION"
                    ),
                )
            position.yesterday_volume -= volume
            yesterday_sold = volume
            today_sold = 0
        else:
            # T+0证券可使用今仓和昨仓中的可用数量。
            from_yesterday = min(position.yesterday_volume, volume)
            position.yesterday_volume -= from_yesterday
            position.today_volume -= volume - from_yesterday
            yesterday_sold = from_yesterday
            today_sold = volume - from_yesterday
        cost = (
            position.position_cost
            if volume == total_before
            else quantize_money(position.position_cost * Decimal(volume) / Decimal(total_before))
        )
        position.frozen_volume -= volume
        position.total_volume -= volume
        position.position_cost = quantize_money(position.position_cost - cost)
        if not getattr(position, "daily_pnl_base_established", True):
            # The legacy part has no provable today/yesterday allocation.  Do
            # not invent one or pro-rate it: preserve that aggregate basis and
            # only remove a known today-bucket amount actually consumed.
            legacy_basis = quantize_money(
                Decimal(getattr(position, "daily_pnl_base_cost", Decimal("0"))) - today_base
            )
            known_today_reduction = (
                today_base
                if today_sold == today_before
                else quantize_money(
                    today_base * Decimal(today_sold) / Decimal(today_before)
                )
                if today_sold
                else Decimal("0")
            )
            position.today_pnl_base_cost = quantize_money(
                today_base - known_today_reduction
            )
            position.daily_pnl_base_cost = quantize_money(
                legacy_basis + position.today_pnl_base_cost
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
        yesterday_reduction = (
            yesterday_base
            if yesterday_sold == yesterday_before
            else quantize_money(
                yesterday_base * Decimal(yesterday_sold)
                / Decimal(yesterday_before)
            )
            if yesterday_sold
            else Decimal("0")
        )
        today_reduction = (
            today_base
            if today_sold == today_before
            else quantize_money(
                today_base * Decimal(today_sold) / Decimal(today_before)
            )
            if today_sold
            else Decimal("0")
        )
        position.yesterday_pnl_base_cost = quantize_money(
            yesterday_base - yesterday_reduction
        )
        position.today_pnl_base_cost = quantize_money(
            today_base - today_reduction
        )
        position.daily_pnl_base_cost = quantize_money(
            Decimal(position.yesterday_pnl_base_cost)
            + Decimal(position.today_pnl_base_cost)
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
