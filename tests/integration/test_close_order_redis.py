import json

import pytest
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
)
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.redis_keys import (
    ACTIVE_ORDERS_ALL_KEY,
    account_active_orders_key,
    active_order_key,
    instrument_active_orders_key,
    processed_order_event_key,
)
from app.models.outbox_event import OutboxEvent
from app.repositories.order_repository import OrderRepository
from app.schemas.order_schema import OrderCancelRequest
from app.services.accepted_order_event_service import AcceptedOrderEventService
from app.services.order_cancellation_service import OrderCancellationService
from app.services.account_access_scope import AccountAccessScope
from tests.integration.test_close_order_lifecycle import (
    create_close_order,
    create_open_position,
    settle,
)


pytestmark = pytest.mark.integration


def process_event(service, event):
    with SessionLocal() as db:
        return service.process(
            db,
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "payload": json.dumps(event.payload),
            },
        )


def test_close_order_rebuild_partial_update_and_cancel_remove(
    integration_context,
):
    try:
        redis_client.ping()
    except (RedisConnectionError, RedisTimeoutError) as exc:
        pytest.skip(f"Redis不可连接: {exc}")

    create_open_position(integration_context)
    order = create_close_order(
        integration_context,
        client_order_id="CLOSE-REDIS",
        volume=4,
    )
    index = ActiveOrderIndex(redis_client)
    service = AcceptedOrderEventService(
        order_repository=OrderRepository(),
        active_order_index=index,
        processed_ttl_seconds=60,
    )
    event_ids = []
    try:
        with SessionLocal() as db:
            accepted_event = db.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == order.order_id,
                    OutboxEvent.event_type == "ORDER_ACCEPTED",
                )
            )
            event_ids.append(accepted_event.event_id)
        assert process_event(service, accepted_event).action == "REGISTERED"
        assert index.get_active_order(order.order_id)["offset_flag"] == (
            "CLOSE_TODAY"
        )

        assert settle(
            order.order_id,
            "TICK-CLOSE-REDIS",
            "3522",
            3,
        ).action == "SETTLED"
        with SessionLocal() as db:
            partial_event = db.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == order.order_id,
                    OutboxEvent.event_type == "ORDER_PARTIALLY_FILLED",
                )
            )
            event_ids.append(partial_event.event_id)
        assert process_event(service, partial_event).action == "UPDATED"
        assert index.get_active_order(order.order_id)[
            "frozen_position_volume"
        ] == "1"

        with SessionLocal() as db:
            OrderCancellationService(
                default_access_scope=AccountAccessScope.admin()
            ).cancel_order(
                db=db,
                order_id=order.order_id,
                request=OrderCancelRequest(
                    account_id=integration_context.account_id
                ),
            )
        with SessionLocal() as db:
            cancel_event = db.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == order.order_id,
                    OutboxEvent.event_type
                    == "ORDER_PARTIALLY_CANCELLED",
                )
            )
            event_ids.append(cancel_event.event_id)
        assert process_event(service, cancel_event).action == "REMOVED"
        assert index.get_active_order(order.order_id) == {}
    finally:
        pipeline = redis_client.pipeline(transaction=True)
        pipeline.delete(active_order_key(order.order_id))
        pipeline.srem(
            instrument_active_orders_key(
                integration_context.exchange_id,
                integration_context.symbol,
            ),
            order.order_id,
        )
        pipeline.srem(
            account_active_orders_key(integration_context.account_id),
            order.order_id,
        )
        pipeline.srem(ACTIVE_ORDERS_ALL_KEY, order.order_id)
        for event_id in event_ids:
            pipeline.delete(processed_order_event_key(event_id))
        pipeline.execute()
