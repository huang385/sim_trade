"""Tick coordinator for stock, convertible-bond and ETF cash orders."""

from typing import Callable, Mapping

from sqlalchemy.orm import Session

from app.enums.order_enums import OrderStatus
from app.enums.instrument_enums import CASH_SECURITY_INSTRUMENT_TYPES
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.repositories.order_repository import OrderRepository
from app.schemas.matching_schema import MarketTickMatchResult
from app.matching.cash_security import (
    CashSecurityMarketSnapshot,
    CashSecurityMatchingStrategy,
    CashSecurityOrderSnapshot,
)
from app.services.cash_security_settlement_service import CashSecuritySettlementService
from app.services.market_tick_event_parser import parse_market_tick_event


class CashSecurityMarketTickMatchingService:
    """Coordinates cash matching without constructing a derivative order."""

    active_statuses = frozenset({
        OrderStatus.ACCEPTED.value,
        OrderStatus.PARTIALLY_FILLED.value,
    })
    instrument_types = CASH_SECURITY_INSTRUMENT_TYPES

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        active_order_index: ActiveOrderIndex,
        order_repository: OrderRepository | None = None,
        matching_strategy: CashSecurityMatchingStrategy | None = None,
        settlement_service: CashSecuritySettlementService | None = None,
        enabled: bool = True,
        instrument_types: frozenset[str] | set[str] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.active_order_index = active_order_index
        self.order_repository = order_repository or OrderRepository()
        self.matching_strategy = matching_strategy or CashSecurityMatchingStrategy()
        self.settlement_service = settlement_service or CashSecuritySettlementService()
        self.enabled = enabled
        self.instrument_types = frozenset(
            CASH_SECURITY_INSTRUMENT_TYPES
            if instrument_types is None
            else instrument_types
        )

    def process(self, *, stream_message_id: str, fields: Mapping[str, str]) -> MarketTickMatchResult:
        event = parse_market_tick_event(fields)
        if not self.enabled:
            return MarketTickMatchResult(0, 0, 0, 0, 0)
        order_ids = sorted(self.active_order_index.list_instrument_order_ids(event.exchange_id, event.symbol))
        return self._process_order_ids(
            order_ids=order_ids,
            stream_message_id=stream_message_id,
            event=event,
        )

    def process_candidate_order(
        self,
        *,
        order_id: str,
        stream_message_id: str,
        event,
        order_snapshot=None,
    ) -> MarketTickMatchResult:
        """Match one newly accepted cash order against a fresh live tick.

        The optional snapshot keeps the order-arrival coordinator compatible
        with the derivative path.  Cash matching always reloads the row under
        its own transaction before settlement.
        """

        _ = order_snapshot
        if not self.enabled:
            return MarketTickMatchResult(0, 0, 0, 0, 0)
        return self._process_order_ids(
            order_ids=[order_id],
            stream_message_id=stream_message_id,
            event=event,
        )

    def process_routed_orders(
        self,
        *,
        order_ids: list[str],
        stream_message_id: str,
        event,
        orders_by_id: Mapping[str, object],
    ) -> MarketTickMatchResult:
        """Match cash candidates supplied by the shared Tick router."""

        return self._process_order_ids(
            order_ids=order_ids,
            stream_message_id=stream_message_id,
            event=event,
            prefetched_orders=orders_by_id,
        )

    def _process_order_ids(
        self,
        *,
        order_ids: list[str],
        stream_message_id: str,
        event,
        prefetched_orders: Mapping[str, object] | None = None,
    ) -> MarketTickMatchResult:
        matched = settled = idempotent = skipped = 0
        first_error = None
        market = CashSecurityMarketSnapshot(
            bid_price_1=event.tick.bid_price_1,
            bid_volume_1=event.tick.bid_volume_1,
            ask_price_1=event.tick.ask_price_1,
            ask_volume_1=event.tick.ask_volume_1,
        )
        for order_id in order_ids:
            try:
                if prefetched_orders is None:
                    with self.session_factory() as db:
                        order = self.order_repository.get_by_order_id(db, order_id)
                        result = self._match_order(order=order, event=event, market=market)
                else:
                    result = self._match_order(
                        order=prefetched_orders.get(order_id),
                        event=event,
                        market=market,
                    )
                if result is None:
                    skipped += 1
                    continue
                if not result.matched:
                    skipped += 1
                    continue
                matched += 1
                with self.session_factory() as settlement_db:
                    settlement = self.settlement_service.settle(
                        settlement_db,
                        order_id=order_id,
                        market_event_id=event.event_id,
                        market_stream_message_id=stream_message_id,
                        tick_event_time=event.tick.event_time,
                        match=result,
                    )
                if settlement.action == "SETTLED":
                    settled += 1
                elif settlement.action == "IDEMPOTENT":
                    idempotent += 1
                else:
                    skipped += 1
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error
        return MarketTickMatchResult(
            candidate_count=len(order_ids),
            matched_count=matched,
            settled_count=settled,
            idempotent_count=idempotent,
            skipped_count=skipped,
        )

    def _match_order(self, *, order, event, market):
        if (
            order is None
            or order.instrument_type not in self.instrument_types
            or order.offset_flag is not None
            or order.status not in self.active_statuses
            or order.remaining_volume <= 0
            or order.exchange_id != event.exchange_id
            or order.symbol != event.symbol
        ):
            return None
        return self.matching_strategy.match(
            CashSecurityOrderSnapshot(
                order_id=order.order_id,
                instrument_type=order.instrument_type,
                direction=order.direction,
                limit_price=order.limit_price,
                remaining_volume=order.remaining_volume,
            ),
            market,
        )
