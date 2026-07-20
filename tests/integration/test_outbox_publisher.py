import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.common.time_utils import utc_now
from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.order_event_publisher import OrderEventPublisher
from app.models.outbox_event import OutboxEvent
from app.repositories.outbox_repository import OutboxRepository
from app.workers.outbox_publisher_worker import OutboxPublisherWorker


pytestmark = pytest.mark.integration


def test_pending_outbox_event_reaches_redis_and_becomes_sent(
    integration_context,
):
    try:
        redis_client.ping()
    except Exception as exc:
        pytest.skip(f"Redis不可用: {exc}")

    event_id = f"EVT-IT-{uuid4().hex.upper()}"
    stream_name = f"stream:it:outbox:{uuid4().hex}"
    with SessionLocal() as db:
        OutboxRepository.create_event(
            db,
            event_id=event_id,
            aggregate_type="ORDER",
            aggregate_id=f"O-IT-{uuid4().hex.upper()}",
            event_type="ORDER_ACCEPTED",
            payload={
                "event_id": event_id,
                "event_type": "ORDER_ACCEPTED",
                "account_id": integration_context.account_id,
                "limit_price": "3500.000000",
            },
            created_at=utc_now(),
        )
        db.commit()

    worker = OutboxPublisherWorker(
        session_factory=SessionLocal,
        publisher=OrderEventPublisher(
            redis_client,
            stream_name=stream_name,
        ),
    )
    result = worker.run_once()

    with SessionLocal() as db:
        event = db.scalar(
            select(OutboxEvent).where(OutboxEvent.event_id == event_id)
        )
        assert event.status == "SENT"
        assert event.sent_at is not None

    matching_messages = []
    for message_id, fields in redis_client.xrange(stream_name):
        if fields.get("event_id") == event_id:
            matching_messages.append((message_id, fields))
    try:
        assert result.sent >= 1
        assert len(matching_messages) == 1
        _, fields = matching_messages[0]
        assert fields["event_type"] == "ORDER_ACCEPTED"
        payload = json.loads(fields["payload"])
        assert payload["event_id"] == event_id
        assert payload["limit_price"] == "3500.000000"
    finally:
        # 只清理当前测试写入的 Redis 消息和 Outbox 行。
        # 集成测试使用独立临时Stream，不干扰正在运行的正式Consumer Group。
        redis_client.delete(stream_name)
        with SessionLocal() as db:
            event = db.scalar(
                select(OutboxEvent).where(OutboxEvent.event_id == event_id)
            )
            if event is not None:
                db.delete(event)
                db.commit()
