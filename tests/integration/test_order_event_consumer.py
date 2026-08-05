import json
from uuid import uuid4

import pytest
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.order_event_publisher import OrderEventPublisher
from app.infrastructure.order_stream_consumer import OrderStreamConsumer
from app.infrastructure.redis_keys import (
    ACTIVE_ORDERS_ALL_KEY,
    account_active_orders_key,
    active_order_key,
    instrument_active_orders_key,
    processed_order_event_key,
)
from app.main import app
from app.models.outbox_event import OutboxEvent
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.services.accepted_order_event_service import AcceptedOrderEventService
from app.workers.order_event_consumer_worker import OrderEventConsumerWorker
from app.workers.outbox_publisher_worker import OutboxPublisherWorker


pytestmark = pytest.mark.integration


def require_redis_connection():
    """仅在Redis确实无法建立连接或响应超时时跳过集成测试。"""

    try:
        redis_client.ping()
    except (RedisConnectionError, RedisTimeoutError) as exc:
        pytest.skip(f"Redis不可连接: {exc}")


class EventScopedOutboxRepository(OutboxRepository):
    """只领取本测试事件，避免共享数据库中的历史Outbox干扰验收。"""

    def __init__(self, event_id: str):
        self.event_id = event_id

    def claim_pending_events(self, db, **_kwargs):
        event = db.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.event_id == self.event_id)
            .with_for_update()
        )
        if event is None:
            return []
        event.status = "PROCESSING"
        db.flush()
        return [event]


def test_api_outbox_stream_consumer_registers_active_order(integration_context):
    """验证 HTTP 下单到 Redis 活动订单索引的完整关键链路。"""

    require_redis_connection()

    suffix = uuid4().hex[:12]
    stream_name = f"stream:it:orders:{suffix}"
    group_name = f"group:it:{suffix}"
    consumer_name = f"consumer:it:{suffix}"
    stream_consumer = OrderStreamConsumer(
        redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name=consumer_name,
        dead_letter_stream=settings.order_dead_letter_stream,
        failure_ttl_seconds=settings.order_event_failure_ttl_seconds,
    )
    # Group 从0-0创建；即便已有消息也不会漏消费。
    stream_consumer.ensure_group()

    response = TestClient(app).post(
        "/api/orders",
        json={
            "client_order_id": f"CONSUMER-{suffix}",
            "account_id": integration_context.account_id,
            "exchange_id": integration_context.exchange_id,
            "symbol": integration_context.symbol,
            "direction": "BUY",
            "offset_flag": "OPEN",
            "order_type": "LIMIT",
            "limit_price": "3500",
            "volume": 2,
        },
    )
    assert response.status_code == 200
    assert response.json()["cancelled_volume"] == 0
    order_id = response.json()["order_id"]

    with SessionLocal() as db:
        outbox_event = db.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == order_id
            )
        )
        event_id = outbox_event.event_id

    OutboxPublisherWorker(
        session_factory=SessionLocal,
        publisher=OrderEventPublisher(
            redis_client,
            stream_name=stream_name,
        ),
        outbox_repository=EventScopedOutboxRepository(event_id),
    ).run_once()

    active_index = ActiveOrderIndex(redis_client)
    event_service = AcceptedOrderEventService(
        order_repository=OrderRepository(),
        active_order_index=active_index,
        processed_ttl_seconds=settings.order_event_processed_ttl_seconds,
    )
    consumer_worker = OrderEventConsumerWorker(
        session_factory=SessionLocal,
        stream_consumer=stream_consumer,
        event_service=event_service,
        batch_size=100,
        block_ms=1,
        pending_idle_ms=0,
        max_retries=10,
        retry_interval_seconds=0,
    )

    try:
        result = consumer_worker.run_once()

        detail = active_index.get_active_order(order_id)
        instrument_ids = active_index.list_instrument_order_ids(
            integration_context.exchange_id,
            integration_context.symbol,
        )
        account_ids = active_index.list_account_order_ids(
            integration_context.account_id
        )
        all_ids = active_index.list_all_order_ids()
        pending = redis_client.xpending(stream_name, group_name)

        assert result.acknowledged >= 1
        assert detail["order_id"] == order_id
        assert detail["status"] == "ACCEPTED"
        assert detail["limit_price"] == "3500.000000"
        assert detail["average_price"] == ""
        assert detail["cancelled_volume"] == "0"
        assert order_id in instrument_ids
        assert order_id in account_ids
        assert order_id in all_ids
        assert redis_client.scard(
            instrument_active_orders_key(
                integration_context.exchange_id,
                integration_context.symbol,
            )
        ) == 1
        assert pending["pending"] == 0

        # 模拟至少一次投递：同一 event_id 作为新消息再次出现。
        # 先故意制造陈旧详情，重复消费应以 PostgreSQL 快照将其修复。
        redis_client.hset(
            active_order_key(order_id),
            mapping={"limit_price": "1.000000"},
        )
        redis_client.xadd(
            stream_name,
            fields={
                "event_id": event_id,
                "event_type": "ORDER_ACCEPTED",
                "payload": json.dumps(outbox_event.payload),
            },
        )
        duplicate_result = consumer_worker.run_once()
        assert duplicate_result.acknowledged >= 1
        assert redis_client.scard(
            instrument_active_orders_key(
                integration_context.exchange_id,
                integration_context.symbol,
            )
        ) == 1
        assert active_index.get_active_order(order_id)["limit_price"] == (
            "3500.000000"
        )
    finally:
        message_ids = [
            message_id
            for message_id, fields in redis_client.xrange(stream_name)
            if fields.get("event_id") == event_id
        ]
        pipeline = redis_client.pipeline(transaction=True)
        pipeline.delete(active_order_key(order_id))
        pipeline.srem(
            instrument_active_orders_key(
                integration_context.exchange_id,
                integration_context.symbol,
            ),
            order_id,
        )
        pipeline.srem(
            account_active_orders_key(integration_context.account_id),
            order_id,
        )
        pipeline.delete(processed_order_event_key(event_id))
        pipeline.srem(ACTIVE_ORDERS_ALL_KEY, order_id)
        if message_ids:
            pipeline.xdel(stream_name, *message_ids)
        pipeline.execute()
        redis_client.delete(stream_name)


def test_group_created_after_message_reads_history_and_keeps_position():
    """0-0创建Group应读取历史消息，BUSYGROUP不能重置已有位置。"""

    require_redis_connection()
    suffix = uuid4().hex[:12]
    stream_name = f"stream:it:group-history:{suffix}"
    group_name = f"group:it:history:{suffix}"
    consumer = OrderStreamConsumer(
        redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name="consumer-history",
        dead_letter_stream=f"{stream_name}:dead",
        failure_ttl_seconds=60,
    )
    first_id = redis_client.xadd(
        stream_name,
        fields={"event_id": "EVT-BEFORE", "payload": "{}"},
    )
    try:
        consumer.ensure_group()
        first_messages = consumer.read_new_messages(
            batch_size=10,
            block_ms=1,
        )
        assert [message_id for message_id, _ in first_messages] == [first_id]
        consumer.acknowledge(first_id)
        assert redis_client.xpending(stream_name, group_name)["pending"] == 0

        second_id = redis_client.xadd(
            stream_name,
            fields={"event_id": "EVT-AFTER", "payload": "{}"},
        )
        # 再次调用只能命中BUSYGROUP并保持原位置，不能回到0-0。
        consumer.ensure_group()
        second_messages = consumer.read_new_messages(
            batch_size=10,
            block_ms=1,
        )
        assert [message_id for message_id, _ in second_messages] == [second_id]
        consumer.acknowledge(second_id)
        assert redis_client.xpending(stream_name, group_name)["pending"] == 0
    finally:
        redis_client.delete(stream_name, f"{stream_name}:dead")


def test_redis5_pending_message_is_recovered_with_compatibility_fallback():
    """本机 Redis 5 不支持 XAUTOCLAIM 时，XPENDING + XCLAIM 仍可恢复。"""

    require_redis_connection()
    suffix = uuid4().hex[:12]
    stream_name = f"stream:it:pending:{suffix}"
    group_name = f"group:it:pending:{suffix}"
    first_consumer = OrderStreamConsumer(
        redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name="consumer-a",
        dead_letter_stream=f"{stream_name}:dead",
        failure_ttl_seconds=60,
    )
    second_consumer = OrderStreamConsumer(
        redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name="consumer-b",
        dead_letter_stream=f"{stream_name}:dead",
        failure_ttl_seconds=60,
    )
    first_consumer.ensure_group()
    message_id = redis_client.xadd(
        stream_name,
        fields={
            "event_id": "EVT-PENDING",
            "event_type": "ORDER_ACCEPTED",
            "payload": "{}",
        },
    )
    first_messages = first_consumer.read_new_messages(
        batch_size=1,
        block_ms=1,
    )
    assert first_messages[0][0] == message_id

    try:
        recovered = second_consumer.claim_stale_messages(
            pending_idle_ms=0,
            batch_size=10,
        )
        assert recovered[0][0] == message_id
        assert redis_client.xpending(stream_name, group_name)["pending"] == 1
        second_consumer.acknowledge(message_id)
        assert redis_client.xpending(stream_name, group_name)["pending"] == 0
    finally:
        redis_client.delete(stream_name, f"{stream_name}:dead")
