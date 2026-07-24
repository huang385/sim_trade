from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.main import app
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.market_tick_stream_consumer import MarketTickStreamConsumer
from app.infrastructure.order_event_publisher import OrderEventPublisher
from app.infrastructure.order_stream_consumer import OrderStreamConsumer
from app.infrastructure.redis_keys import (
    ACTIVE_ORDERS_ALL_KEY,
    account_active_orders_key,
    active_order_key,
    instrument_active_orders_key,
    market_latest_key,
    processed_order_event_key,
)
from app.models.account import Account
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.models.trade import Trade
from app.matching.engines.vn import VnMatchingEngine
from app.repositories.order_repository import OrderRepository
from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType
from app.services.accepted_order_event_service import AcceptedOrderEventService
from app.services.market_tick_matching_service import MarketTickMatchingService
from app.services.trade_settlement_service import TradeSettlementService
from app.workers.matching_worker import MatchingWorker
from app.workers.order_event_consumer_worker import OrderEventConsumerWorker
from tests.integration.conftest import make_order_service, make_request


pytestmark = pytest.mark.integration


def _tick(context, event_id: str, price: str, volume: int, sequence: int):
    return MarketTick(
        source_event_id=event_id,
        ingest_type=MarketTickIngestType.LIVE_CALLBACK,
        order_book_id=context.symbol,
        exchange_id=context.exchange_id,
        symbol=context.symbol,
        trading_day=context.trading_day,
        event_time=datetime(2026, 7, 23, 1, sequence, tzinfo=timezone.utc),
        sequence_id=sequence,
        cumulative_volume=sequence,
        bid_price_1=Decimal(price) - Decimal("1"),
        bid_volume_1=volume,
        ask_price_1=Decimal(price),
        ask_volume_1=volume,
    )


def test_real_postgres_redis_partial_then_full_matching(integration_context):
    """验证接单索引、两次撮合结算、状态事件和最终索引删除的闭环。"""

    try:
        redis_client.ping()
    except Exception as exc:
        pytest.skip(f"Redis不可用: {exc}")

    suffix = uuid4().hex[:10]
    order_stream = f"stream:it:matching-orders:{suffix}"
    order_group = f"group:it:matching-orders:{suffix}"
    market_stream = f"stream:it:matching-ticks:{suffix}"
    market_group = f"group:it:matching-ticks:{suffix}"
    dead_stream = f"stream:it:matching-dead:{suffix}"
    active_index = ActiveOrderIndex(redis_client)
    published_event_ids: set[str] = set()
    order_id = ""

    order_consumer = OrderStreamConsumer(
        redis_client,
        stream_name=order_stream,
        group_name=order_group,
        consumer_name=f"order-{suffix}",
        dead_letter_stream=dead_stream,
        failure_ttl_seconds=60,
    )
    order_consumer.ensure_group()
    order_worker = OrderEventConsumerWorker(
        session_factory=SessionLocal,
        stream_consumer=order_consumer,
        event_service=AcceptedOrderEventService(
            order_repository=OrderRepository(),
            active_order_index=active_index,
            processed_ttl_seconds=60,
        ),
        batch_size=100,
        block_ms=1,
        pending_idle_ms=60000,
        max_retries=10,
        retry_interval_seconds=0,
    )
    market_consumer = MarketTickStreamConsumer(
        redis_client,
        stream_name=market_stream,
        group_name=market_group,
        consumer_name=f"matching-{suffix}",
        dead_letter_stream=dead_stream,
        failure_ttl_seconds=60,
    )
    market_consumer.ensure_group()
    matching_worker = MatchingWorker(
        stream_consumer=market_consumer,
        matching_service=MarketTickMatchingService(
            session_factory=SessionLocal,
            active_order_index=active_index,
            order_repository=OrderRepository(),
            matching_engine=VnMatchingEngine(),
            settlement_service=TradeSettlementService(),
        ),
        batch_size=100,
        block_ms=1,
        pending_idle_ms=60000,
        max_retries=10,
        retry_interval_seconds=0,
    )
    publisher = OrderEventPublisher(redis_client, stream_name=order_stream)

    def publish_new_outbox_events():
        with SessionLocal() as db:
            events = db.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.aggregate_id.in_([order_id] + [
                    item.trade_id
                    for item in db.scalars(
                        select(Trade).where(Trade.order_id == order_id)
                    ).all()
                ]))
                .order_by(OutboxEvent.id)
            ).all()
            for event in events:
                if event.event_id not in published_event_ids:
                    publisher.publish(event)
                    published_event_ids.add(event.event_id)

    try:
        with SessionLocal() as db:
            order = make_order_service(integration_context).create_order(
                db,
                make_request(
                    integration_context,
                    client_order_id=f"MATCH-{suffix}",
                    volume=2,
                ),
            )
            order_id = order.order_id

        publish_new_outbox_events()
        assert order_worker.run_once().acknowledged == 1
        assert order_id in active_index.list_instrument_order_ids(
            integration_context.exchange_id, integration_context.symbol
        )

        tick_store = MarketTickStore(redis_client, stream_name=market_stream)
        tick_store.publish(_tick(integration_context, f"TICK-{suffix}-1", "3499", 1, 1))
        assert matching_worker.run_once().acknowledged == 1
        publish_new_outbox_events()
        assert order_worker.run_once().acknowledged == 2
        with SessionLocal() as db:
            partial = db.scalar(select(Order).where(Order.order_id == order_id))
            assert partial.status == "PARTIALLY_FILLED"
            assert partial.remaining_volume == 1
        assert active_index.get_active_order(order_id)["remaining_volume"] == "1"

        tick_store.publish(_tick(integration_context, f"TICK-{suffix}-2", "3500", 10, 2))
        assert matching_worker.run_once().acknowledged == 1
        publish_new_outbox_events()
        assert order_worker.run_once().acknowledged == 2

        with SessionLocal() as db:
            order = db.scalar(select(Order).where(Order.order_id == order_id))
            account = db.scalar(
                select(Account).where(
                    Account.account_id == integration_context.account_id
                )
            )
            position = db.scalar(
                select(Position).where(
                    Position.account_id == integration_context.account_id
                )
            )
            assert order.status == "FILLED"
            assert order.remaining_volume == 0
            assert len(db.scalars(select(Trade).where(Trade.order_id == order_id)).all()) == 2
            assert len(db.scalars(select(PositionDetail).where(PositionDetail.account_id == integration_context.account_id)).all()) == 2
            assert position.total_volume == 2
            assert account.frozen_margin == Decimal("0.000000")
            assert account.used_margin == Decimal("8400.000000")
            assert account.used_commission == Decimal("6.000000")
            assert account.available_cash == Decimal("91594.000000")

        # 查询 API 只读数据库，不包含撮合、资金或 Redis 逻辑。
        client = TestClient(app)
        trade_response = client.get(
            "/api/trades", params={"order_id": order_id}
        )
        assert trade_response.status_code == 200
        assert len(trade_response.json()) == 2
        trade_id = trade_response.json()[0]["trade_id"]
        assert client.get(f"/api/trades/{trade_id}").status_code == 200
        position_response = client.get(
            "/api/positions",
            params={"account_id": integration_context.account_id},
        )
        assert position_response.status_code == 200
        assert position_response.json()[0]["total_volume"] == 2
        assert active_index.get_active_order(order_id) == {}
        assert order_id not in active_index.list_instrument_order_ids(
            integration_context.exchange_id, integration_context.symbol
        )
        pending = redis_client.xpending(market_stream, market_group)
        assert pending["pending"] == 0
    finally:
        cleanup = [
            order_stream,
            market_stream,
            dead_stream,
            market_latest_key(
                integration_context.exchange_id, integration_context.symbol
            ),
        ]
        if order_id:
            cleanup.extend(
                [
                    active_order_key(order_id),
                    account_active_orders_key(integration_context.account_id),
                    instrument_active_orders_key(
                        integration_context.exchange_id,
                        integration_context.symbol,
                    ),
                ]
            )
        cleanup.extend(processed_order_event_key(item) for item in published_event_ids)
        redis_client.delete(*cleanup)
        if order_id:
            redis_client.srem(ACTIVE_ORDERS_ALL_KEY, order_id)
