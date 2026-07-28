import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.redis_keys import (
    ACTIVE_ORDERS_ALL_KEY,
    account_active_orders_key,
    active_order_key,
    instrument_active_orders_key,
    processed_order_event_key,
)
from app.matching.engines.vn import VnMatchingEngine
from app.models.order import Order
from app.models.trade import Trade
from app.repositories.order_repository import OrderRepository
from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType
from app.services.accepted_order_event_service import (
    AcceptedOrderEventService,
)
from app.services.live_market_snapshot_service import LiveMatchingEvent
from app.services.market_tick_matching_service import (
    MarketTickMatchingService,
)
from app.services.order_arrival_matching_service import (
    OrderArrivalMatchingService,
)
from app.services.trade_settlement_service import TradeSettlementService
from tests.integration.conftest import make_order_service, make_request


pytestmark = pytest.mark.integration


def test_accepted_order_matches_against_current_live_cached_tick(
    integration_context,
):
    """没有后续价格变化时，订单到达仍应使用当前代实时盘口立即成交。"""

    try:
        redis_client.ping()
    except Exception as exc:
        pytest.skip(f"Redis不可用: {exc}")

    suffix = uuid4().hex[:10].upper()
    event_id = f"ORDER-ARRIVAL-{suffix}"
    order_id = ""
    active_index = ActiveOrderIndex(redis_client)
    try:
        with SessionLocal() as db:
            order = make_order_service(
                integration_context
            ).create_order(
                db,
                make_request(
                    integration_context,
                    client_order_id=f"ARRIVAL-{suffix}",
                    limit_price=Decimal("3500"),
                    volume=2,
                ),
            )
            order_id = order.order_id

        fields = {
            "event_id": event_id,
            "event_type": "ORDER_ACCEPTED",
            "payload": json.dumps(
                {
                    "order_id": order_id,
                    "account_id": integration_context.account_id,
                    "exchange_id": integration_context.exchange_id,
                    "symbol": integration_context.symbol,
                }
            ),
        }
        with SessionLocal() as db:
            index_result = AcceptedOrderEventService(
                order_repository=OrderRepository(),
                active_order_index=active_index,
                processed_ttl_seconds=60,
            ).process(db, fields)
        assert index_result.action == "REGISTERED"

        tick = MarketTick(
            source_event_id=f"TICK-ARRIVAL-{suffix}",
            ingest_type=MarketTickIngestType.LIVE_CALLBACK,
            order_book_id=integration_context.symbol,
            exchange_id=integration_context.exchange_id,
            symbol=integration_context.symbol,
            trading_day=integration_context.trading_day,
            event_time=datetime(2026, 7, 28, 6, 0, tzinfo=timezone.utc),
            sequence_id=1,
            cumulative_volume=1,
            bid_price_1=Decimal("3499"),
            bid_volume_1=10,
            ask_price_1=Decimal("3500"),
            ask_volume_1=10,
        )
        snapshot_service = Mock()
        snapshot_service.get_matching_event.return_value = (
            LiveMatchingEvent(
                stream_message_id=f"arrival-{suffix}-0",
                fields={
                    "event_id": tick.source_event_id,
                    "event_type": "MARKET_TICK",
                    "exchange_id": tick.exchange_id,
                    "symbol": tick.symbol,
                    "order_book_id": tick.order_book_id,
                    "payload": MarketTickStore.tick_to_payload(tick),
                },
            )
        )
        result = OrderArrivalMatchingService(
            live_market_snapshot_service=snapshot_service,
            matching_service=MarketTickMatchingService(
                session_factory=SessionLocal,
                active_order_index=active_index,
                order_repository=OrderRepository(),
                matching_engine=VnMatchingEngine(),
                settlement_service=TradeSettlementService(),
            ),
        ).match_if_ready(
            exchange_id=integration_context.exchange_id,
            symbol=integration_context.symbol,
        )

        assert result.action == "SETTLED"
        with SessionLocal() as db:
            stored_order = db.scalar(
                select(Order).where(Order.order_id == order_id)
            )
            trades = db.scalars(
                select(Trade).where(Trade.order_id == order_id)
            ).all()
            assert stored_order.status == "FILLED"
            assert stored_order.remaining_volume == 0
            assert len(trades) == 1
            assert trades[0].market_event_id == tick.source_event_id
            assert trades[0].market_stream_message_id == (
                f"arrival-{suffix}-0"
            )
    finally:
        if order_id:
            redis_client.delete(
                active_order_key(order_id),
                account_active_orders_key(
                    integration_context.account_id
                ),
                instrument_active_orders_key(
                    integration_context.exchange_id,
                    integration_context.symbol,
                ),
                processed_order_event_key(event_id),
            )
            redis_client.srem(ACTIVE_ORDERS_ALL_KEY, order_id)
