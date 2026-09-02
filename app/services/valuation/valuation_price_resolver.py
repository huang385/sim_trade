from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum

from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.services.trading_day_service import (
    TradingDayService,
    TradingSessionState,
    get_trading_day_service,
)


class ValuationPriceSource(str, Enum):
    REALTIME = "REALTIME"
    SETTLEMENT = "SETTLEMENT"


@dataclass(frozen=True)
class ResolvedValuationPrice:
    price: Decimal
    source: ValuationPriceSource


class ValuationPriceResolver:
    """在实时行情与可信结算基准之间选择持仓估值价格。"""

    def __init__(
        self,
        trading_day_service: TradingDayService | None = None,
    ) -> None:
        self.trading_day_service = (
            trading_day_service or get_trading_day_service()
        )

    @staticmethod
    def realtime_price(
        values: dict[str, str],
        *,
        expected_trading_day: date | None,
    ) -> Decimal | None:
        if not values:
            return None
        try:
            tick = MarketTickStore.mapping_to_tick(values)
        except Exception:
            # 兼容只保存核心字段的历史快照和隔离测试替身，但来源、交易日、
            # 有限正数价格仍必须全部满足。
            if (
                (values.get("source"), values.get("ingest_type"))
                not in {
                    ("YMM_LIVE_DATA", "LIVE_CALLBACK"),
                    ("YMM_DATA_SDK", "REST_SNAPSHOT"),
                }
                or (
                    expected_trading_day is not None
                    and values.get("trading_day")
                    != expected_trading_day.isoformat()
                )
            ):
                return None
            try:
                price = Decimal(values.get("last_price", ""))
            except Exception:
                return None
            return price if price.is_finite() and price > 0 else None
        if (
            tick.last_price is None
            or tick.last_price <= 0
            or (
                expected_trading_day is not None
                and tick.trading_day != expected_trading_day
            )
        ):
            return None
        return tick.last_price

    @staticmethod
    def settlement_price(
        details,
        *,
        expected_trading_day: date | None,
    ) -> Decimal | None:
        """只接受已跨日结转且所有有效明细一致的结算基准。"""

        if expected_trading_day is None:
            return None
        active = [item for item in details if item.remaining_volume > 0]
        if not active or any(
            getattr(item, "open_trading_day", None) is None
            or item.open_trading_day >= expected_trading_day
            for item in active
        ):
            return None
        try:
            prices = {Decimal(item.pnl_base_price) for item in active}
        except (AttributeError, TypeError, ArithmeticError):
            return None
        if len(prices) != 1:
            return None
        price = next(iter(prices))
        return price if price.is_finite() and price > 0 else None

    def resolve_position(
        self,
        db,
        *,
        instrument,
        market_values: dict[str, str],
        details,
        expected_trading_day: date | None,
    ) -> ResolvedValuationPrice | None:
        realtime = self.realtime_price(
            market_values,
            expected_trading_day=expected_trading_day,
        )
        if realtime is not None:
            return ResolvedValuationPrice(
                realtime, ValuationPriceSource.REALTIME
            )
        baseline = self.settlement_price(
            details,
            expected_trading_day=expected_trading_day,
        )
        if baseline is None or expected_trading_day is None:
            return None
        if (
            self.trading_day_service.session_state(
                db,
                instrument=instrument,
                trading_day=expected_trading_day,
            )
            != TradingSessionState.CLOSED
        ):
            return None
        return ResolvedValuationPrice(
            baseline, ValuationPriceSource.SETTLEMENT
        )
