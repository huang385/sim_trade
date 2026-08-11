from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.common.exceptions import DataAccessError
from app.enums.order_enums import OrderType
from app.repositories.order_repository import OrderRepository
from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType
from app.services.market_tick_matching_service import (
    MarketTickMatchingService,
    ParsedMarketTickEvent,
)
from app.services.order_cancellation_service import OrderCancellationService


@dataclass(frozen=True)
class MarketOrderExecutionResult:
    settled_count: int
    cancelled_remainder: bool


class MarketOrderExecutionService:
    """Match a market order once against its persisted level-one snapshot."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        matching_service: MarketTickMatchingService,
        cancellation_service: OrderCancellationService,
        order_repository: OrderRepository | None = None,
    ):
        self.session_factory = session_factory
        self.matching_service = matching_service
        self.cancellation_service = cancellation_service
        self.order_repository = order_repository or OrderRepository()

    def execute(self, *, order_id: str, order_snapshot=None) -> MarketOrderExecutionResult:
        with self.session_factory() as db:
            order = self.order_repository.get_by_order_id(db, order_id)
            if order is None or order.order_type != OrderType.MARKET.value:
                raise DataAccessError(
                    "市价单事实不存在", error_code="MARKET_ORDER_FACT_MISSING"
                )
            required = (
                order.price_snapshot_time,
                order.price_snapshot_source,
                order.price_snapshot_event_id,
                order.price_snapshot_stream_message_id,
                order.market_protection_price,
            )
            if any(value is None for value in required):
                raise DataAccessError(
                    "市价单行情快照不完整",
                    error_code="MARKET_ORDER_SNAPSHOT_INCOMPLETE",
                )
            tick = MarketTick(
                source_event_id=order.price_snapshot_event_id,
                source=order.price_snapshot_source,
                ingest_type=MarketTickIngestType.LIVE_CALLBACK,
                order_book_id=order.order_book_id,
                exchange_id=order.exchange_id,
                symbol=order.symbol,
                trading_day=order.trading_day,
                event_time=order.price_snapshot_time,
                sequence_id=0,
                last_price=order.price_snapshot_last,
                cumulative_volume=0,
                bid_price_1=order.price_snapshot_bid1,
                bid_volume_1=order.price_snapshot_bid_volume1 or 0,
                ask_price_1=order.price_snapshot_ask1,
                ask_volume_1=order.price_snapshot_ask_volume1 or 0,
            )
            stream_message_id = order.price_snapshot_stream_message_id
            parsed = ParsedMarketTickEvent(
                event_id=order.price_snapshot_event_id,
                exchange_id=order.exchange_id,
                symbol=order.symbol,
                tick=tick,
            )

        match_result = self.matching_service.process_candidate_order(
            order_id=order_id,
            stream_message_id=stream_message_id,
            event=parsed,
            order_snapshot=order_snapshot,
        )
        with self.session_factory() as db:
            current = self.order_repository.get_by_order_id(db, order_id)
            should_cancel = current is not None and current.remaining_volume > 0
            if should_cancel:
                self.cancellation_service.cancel_market_remainder(db, order_id)
        return MarketOrderExecutionResult(
            settled_count=match_result.settled_count,
            cancelled_remainder=should_cancel,
        )
