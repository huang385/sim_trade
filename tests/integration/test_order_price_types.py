import json
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.common.time_utils import utc_now
from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.redis_keys import processed_order_event_key
from app.matching.engines.vn import VnMatchingEngine
from app.models.account import Account
from app.models.order import Order
from app.models.trade import Trade
from app.repositories.order_repository import OrderRepository
from app.services.accepted_order_event_service import AcceptedOrderEventService
from app.services.market_order_execution_service import MarketOrderExecutionService
from app.services.market_tick_matching_service import MarketTickMatchingService
from app.services.order_cancellation_service import OrderCancellationService
from app.services.order_price_resolver import ResolvedOrderPrice
from app.services.trade_settlement_service import TradeSettlementService
from tests.integration.conftest import make_order_service, make_request


pytestmark = pytest.mark.integration


def test_market_order_matches_one_level_then_cancels_and_releases_remainder(
    integration_context,
):
    try:
        redis_client.ping()
    except Exception as exc:
        pytest.skip(f"Redis不可用: {exc}")

    suffix = uuid4().hex[:10].upper()
    event_id = f"MARKET-ACCEPTED-{suffix}"
    resolver = Mock()
    resolver.resolve.return_value = ResolvedOrderPrice(
        resolved_price=Decimal("3600"),
        market_protection_price=Decimal("3600"),
        snapshot_time=utc_now(),
        snapshot_source="YMM_LIVE_DATA",
        snapshot_event_id=f"MARKET-TICK-{suffix}",
        snapshot_stream_message_id=f"market-{suffix}-0",
        bid1=Decimal("3499"),
        bid_volume1=4,
        ask1=Decimal("3500"),
        ask_volume1=1,
        last_price=Decimal("3499"),
    )
    active_index = ActiveOrderIndex(redis_client)
    order_id = ""
    try:
        with SessionLocal() as db:
            order = make_order_service(
                integration_context,
                order_price_resolver=resolver,
            ).create_order(
                db,
                make_request(
                    integration_context,
                    client_order_id=f"MARKET-{suffix}",
                    order_type="MARKET",
                    limit_price=None,
                    volume=3,
                ),
            )
            order_id = order.order_id
            assert order.limit_price == Decimal("3600.000000")
            assert order.frozen_margin > 0

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
            routed = AcceptedOrderEventService(
                order_repository=OrderRepository(),
                active_order_index=active_index,
                processed_ttl_seconds=60,
            ).process(db, fields)
        assert routed.action == "MARKET_READY"
        assert active_index.get_active_order(order_id) == {}

        matching = MarketTickMatchingService(
            session_factory=SessionLocal,
            active_order_index=active_index,
            order_repository=OrderRepository(),
            matching_engine=VnMatchingEngine(),
            settlement_service=TradeSettlementService(),
        )
        result = MarketOrderExecutionService(
            session_factory=SessionLocal,
            matching_service=matching,
            cancellation_service=OrderCancellationService(),
        ).execute(
            order_id=order_id,
            order_snapshot=routed.order_snapshot,
        )
        assert result.settled_count == 1
        assert result.cancelled_remainder is True

        with SessionLocal() as db:
            stored = db.scalar(select(Order).where(Order.order_id == order_id))
            trades = db.scalars(
                select(Trade).where(Trade.order_id == order_id)
            ).all()
            account = db.scalar(
                select(Account).where(
                    Account.account_id == integration_context.account_id
                )
            )
            assert stored.status == "PARTIALLY_CANCELLED"
            assert stored.traded_volume == 1
            assert stored.cancelled_volume == 2
            assert stored.remaining_volume == 0
            assert stored.cancel_reason_code == "MARKET_REMAINDER_CANCELLED"
            assert stored.frozen_margin == Decimal("0.000000")
            assert stored.frozen_commission == Decimal("0.000000")
            assert len(trades) == 1
            assert trades[0].trade_price == Decimal("3500.000000")
            assert account.frozen_margin == Decimal("0.000000")
    finally:
        redis_client.delete(processed_order_event_key(event_id))
