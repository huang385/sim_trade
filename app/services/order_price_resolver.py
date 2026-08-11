from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from app.common.exceptions import BusinessValidationError, ServiceUnavailableError
from app.common.time_utils import utc_now
from app.enums.order_enums import OrderDirection, OrderType
from app.schemas.order_schema import OrderCreateRequest
from app.services.live_market_snapshot_service import LiveMarketSnapshotService


@dataclass(frozen=True)
class ResolvedOrderPrice:
    resolved_price: Decimal
    submitted_limit_price: Decimal | None = None
    market_protection_price: Decimal | None = None
    snapshot_time: datetime | None = None
    snapshot_source: str | None = None
    snapshot_event_id: str | None = None
    snapshot_stream_message_id: str | None = None
    bid1: Decimal | None = None
    bid_volume1: int | None = None
    ask1: Decimal | None = None
    ask_volume1: int | None = None
    last_price: Decimal | None = None


class OrderPriceResolver:
    """Resolve a requested price type once from one acceptance-time snapshot."""

    def __init__(
        self,
        *,
        live_market_snapshot_service: LiveMarketSnapshotService,
        max_age_seconds: int,
        market_max_slippage_rate: Decimal,
    ):
        self.live_market_snapshot_service = live_market_snapshot_service
        self.max_age_seconds = max_age_seconds
        self.market_max_slippage_rate = market_max_slippage_rate
        if not Decimal("0") <= market_max_slippage_rate < Decimal("1"):
            raise ValueError("市价单最大滑点比例必须处于 [0, 1) 区间")

    @staticmethod
    def _round_to_tick(value: Decimal, tick: Decimal, *, upward: bool) -> Decimal:
        rounding = ROUND_CEILING if upward else ROUND_FLOOR
        return (value / tick).to_integral_value(rounding=rounding) * tick

    def resolve(
        self,
        *,
        request: OrderCreateRequest,
        price_tick: Decimal,
        trading_day: date,
    ) -> ResolvedOrderPrice:
        if request.order_type == OrderType.LIMIT:
            assert request.limit_price is not None
            return ResolvedOrderPrice(
                resolved_price=request.limit_price,
                submitted_limit_price=request.limit_price,
            )

        event = self.live_market_snapshot_service.get_matching_event(
            exchange_id=request.exchange_id,
            symbol=request.symbol,
        )
        if event is None:
            raise ServiceUnavailableError(
                "当前没有可用于委托定价的有效实时行情",
                error_code="ORDER_PRICE_MARKET_DATA_UNAVAILABLE",
            )
        tick = event.parsed_event.tick
        if tick.event_time.tzinfo is None:
            raise BusinessValidationError(
                "委托定价行情时间缺少时区",
                error_code="ORDER_PRICE_MARKET_DATA_INVALID",
            )
        age_seconds = (utc_now() - tick.event_time).total_seconds()
        if age_seconds < -1 or age_seconds > self.max_age_seconds:
            raise BusinessValidationError(
                "委托定价行情已过期",
                error_code="ORDER_PRICE_MARKET_DATA_STALE",
            )
        if tick.trading_day != trading_day:
            raise BusinessValidationError(
                "委托定价行情交易日不一致",
                error_code="ORDER_PRICE_TRADING_DAY_MISMATCH",
            )
        quoted = (tick.bid_price_1, tick.ask_price_1, tick.last_price)
        if any(value is not None and value <= 0 for value in quoted):
            raise BusinessValidationError(
                "委托定价行情价格异常",
                error_code="ORDER_PRICE_MARKET_DATA_INVALID",
            )
        if tick.bid_volume_1 < 0 or tick.ask_volume_1 < 0:
            raise BusinessValidationError(
                "委托定价盘口数量异常",
                error_code="ORDER_PRICE_MARKET_DATA_INVALID",
            )
        if (
            tick.bid_price_1 is not None
            and tick.ask_price_1 is not None
            and tick.bid_price_1 > tick.ask_price_1
        ):
            raise BusinessValidationError(
                "委托定价盘口倒挂",
                error_code="ORDER_PRICE_MARKET_DATA_INVALID",
            )

        protection = None
        if request.order_type == OrderType.COUNTERPARTY:
            resolved = (
                tick.ask_price_1
                if request.direction == OrderDirection.BUY
                else tick.bid_price_1
            )
            missing_code = "ASK1_MISSING" if request.direction == OrderDirection.BUY else "BID1_MISSING"
        elif request.order_type == OrderType.LAST:
            resolved = tick.last_price
            missing_code = "LAST_PRICE_MISSING"
        else:
            opposite = (
                tick.ask_price_1
                if request.direction == OrderDirection.BUY
                else tick.bid_price_1
            )
            missing_code = "ASK1_MISSING" if request.direction == OrderDirection.BUY else "BID1_MISSING"
            if opposite is None:
                resolved = None
            else:
                upward = request.direction == OrderDirection.BUY
                factor = (
                    Decimal("1") + self.market_max_slippage_rate
                    if upward
                    else Decimal("1") - self.market_max_slippage_rate
                )
                protection = self._round_to_tick(opposite * factor, price_tick, upward=upward)
                resolved = protection
                if protection <= 0:
                    raise BusinessValidationError(
                        "市价保护价不可用",
                        error_code="MARKET_PROTECTION_PRICE_INVALID",
                    )
        if resolved is None:
            raise BusinessValidationError(
                "委托定价所需盘口字段缺失",
                error_code=missing_code,
            )
        if request.order_type in {OrderType.COUNTERPARTY, OrderType.MARKET}:
            opposite_volume = (
                tick.ask_volume_1
                if request.direction == OrderDirection.BUY
                else tick.bid_volume_1
            )
            if opposite_volume <= 0:
                raise BusinessValidationError(
                    "委托定价所需盘口没有可用数量",
                    error_code="ORDER_PRICE_BOOK_EMPTY",
                )

        return ResolvedOrderPrice(
            resolved_price=resolved,
            market_protection_price=protection,
            snapshot_time=tick.event_time,
            snapshot_source=tick.source,
            snapshot_event_id=tick.source_event_id,
            snapshot_stream_message_id=event.stream_message_id,
            bid1=tick.bid_price_1,
            bid_volume1=tick.bid_volume_1,
            ask1=tick.ask_price_1,
            ask_volume1=tick.ask_volume_1,
            last_price=tick.last_price,
        )
