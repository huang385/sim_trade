from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import or_, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.common.time_utils import utc_now
from app.enums.order_enums import OutboxStatus
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
from app.matching.models import MatchResult
from app.models.account import Account
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.position import Position
from app.models.trade import Trade
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.services.accepted_order_event_service import AcceptedOrderEventService
from app.services.trade_settlement_service import (
    SettlementCommand,
    TradeSettlementService,
)
from app.workers.order_event_consumer_worker import OrderEventConsumerWorker
from app.workers.outbox_publisher_worker import OutboxPublisherWorker
from tests.integration.conftest import make_order_service, make_request


pytestmark = pytest.mark.integration


class ScopedOutboxRepository(OutboxRepository):
    """仅领取本测试明确登记的事件，避免触碰用户已有PENDING Outbox。"""

    def __init__(self):
        self.event_ids: set[str] = set()

    def claim_pending_events(
        self,
        db,
        *,
        batch_size=100,
        now=None,
        processing_timeout_seconds=60,
    ):
        if not self.event_ids:
            return []
        current_time = now or utc_now()
        events = db.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.event_id.in_(self.event_ids),
                OutboxEvent.status == OutboxStatus.PENDING.value,
                or_(
                    OutboxEvent.next_retry_at.is_(None),
                    OutboxEvent.next_retry_at <= current_time,
                ),
            )
            .order_by(OutboxEvent.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        lease_deadline = current_time + timedelta(
            seconds=processing_timeout_seconds
        )
        for event in events:
            event.status = OutboxStatus.PROCESSING.value
            event.next_retry_at = lease_deadline
            event.updated_at = current_time
        db.flush()
        return events


class FailingPublisher:
    """模拟Redis发布连接失败，不接触真实Stream。"""

    @staticmethod
    def publish(_event):
        raise ConnectionError("redis unavailable")


def test_partial_fill_cancel_outbox_consumer_removes_active_index(
    integration_context,
):
    """
    验证10手下单、成交3手、撤销7手、Outbox补发和Redis索引删除闭环。

    撤单数据库事务先独立成功并留下PENDING事件，随后才启动发布和消费，
    等价覆盖Redis暂时不可用后恢复补发的最终一致性路径。
    """

    try:
        redis_client.ping()
    except Exception as exc:
        pytest.skip(f"Redis不可用: {exc}")

    suffix = uuid4().hex[:10]
    stream_name = f"stream:it:cancel:{suffix}"
    group_name = f"group:it:cancel:{suffix}"
    consumer = OrderStreamConsumer(
        redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name=f"cancel-consumer-{suffix}",
        dead_letter_stream=f"{stream_name}:dead",
        failure_ttl_seconds=60,
    )
    consumer.ensure_group()
    active_index = ActiveOrderIndex(redis_client)
    event_service = AcceptedOrderEventService(
        order_repository=OrderRepository(),
        active_order_index=active_index,
        processed_ttl_seconds=60,
    )
    consumer_worker = OrderEventConsumerWorker(
        session_factory=SessionLocal,
        stream_consumer=consumer,
        event_service=event_service,
        batch_size=100,
        block_ms=1,
        pending_idle_ms=0,
        max_retries=10,
        retry_interval_seconds=0,
    )
    scoped_outbox_repository = ScopedOutboxRepository()
    publisher_worker = OutboxPublisherWorker(
        session_factory=SessionLocal,
        publisher=OrderEventPublisher(
            redis_client,
            stream_name=stream_name,
        ),
        outbox_repository=scoped_outbox_repository,
        batch_size=100,
        idle_seconds=0,
    )
    order_id = ""
    event_ids: list[str] = []

    try:
        with SessionLocal() as db:
            order = make_order_service(integration_context).create_order(
                db,
                make_request(
                    integration_context,
                    client_order_id=f"CANCEL-E2E-{suffix}",
                    volume=10,
                ),
            )
            order_id = order.order_id
            accepted_event_id = db.scalar(
                select(OutboxEvent.event_id).where(
                    OutboxEvent.aggregate_id == order_id,
                    OutboxEvent.event_type == "ORDER_ACCEPTED",
                )
            )
            scoped_outbox_repository.event_ids = {accepted_event_id}

        # 先发布并消费ORDER_ACCEPTED，证明撤单前订单确实存在于活动索引。
        assert publisher_worker.run_once().sent >= 1
        assert consumer_worker.run_once().acknowledged >= 1
        assert order_id in active_index.list_all_order_ids()

        with SessionLocal() as db:
            settlement = TradeSettlementService().settle(
                db,
                SettlementCommand(
                    order_id=order_id,
                    market_event_id=f"TICK-CANCEL-{suffix}",
                    market_stream_message_id=f"TICK-CANCEL-{suffix}-0",
                    tick_event_time=datetime(
                        2026,
                        7,
                        24,
                        1,
                        tzinfo=timezone.utc,
                    ),
                    tick_sequence_id=1,
                    match_result=MatchResult(
                        matched=True,
                        fill_price=Decimal("3499"),
                        fill_volume=3,
                        reason=None,
                        engine_name="VN",
                        engine_version="1.0",
                    ),
                ),
            )
            assert settlement.action == "SETTLED"

        cancel_response = TestClient(app).post(
            f"/api/orders/{order_id}/cancel",
            json={"account_id": integration_context.account_id},
        )
        assert cancel_response.status_code == 200
        assert cancel_response.json()["status"] == "PARTIALLY_CANCELLED"

        # API没有访问Redis；此时旧活动索引仍在，撤单事件可靠停留在PENDING。
        assert order_id in active_index.list_all_order_ids()
        with SessionLocal() as db:
            cancel_event = db.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == order_id,
                    OutboxEvent.event_type
                    == "ORDER_PARTIALLY_CANCELLED",
                )
            )
            assert cancel_event.status == "PENDING"
            trade_id = db.scalar(
                select(Trade.trade_id).where(Trade.order_id == order_id)
            )
            scoped_outbox_repository.event_ids = set(
                db.scalars(
                    select(OutboxEvent.event_id).where(
                        (
                            (OutboxEvent.aggregate_type == "ORDER")
                            & (OutboxEvent.aggregate_id == order_id)
                        )
                        | (
                            (OutboxEvent.aggregate_type == "TRADE")
                            & (OutboxEvent.aggregate_id == trade_id)
                        )
                    )
                ).all()
            )

        # 明确模拟Redis发布失败：数据库撤单不能回滚，事件进入PENDING重试。
        failed_publish = OutboxPublisherWorker(
            session_factory=SessionLocal,
            publisher=FailingPublisher(),
            outbox_repository=scoped_outbox_repository,
            batch_size=100,
            idle_seconds=0,
        ).run_once()
        assert failed_publish.retried >= 1
        with SessionLocal() as db:
            order_after_failure = db.scalar(
                select(Order).where(Order.order_id == order_id)
            )
            cancel_event_after_failure = db.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == order_id,
                    OutboxEvent.event_type
                    == "ORDER_PARTIALLY_CANCELLED",
                )
            )
            assert order_after_failure.status == "PARTIALLY_CANCELLED"
            assert cancel_event_after_failure.status == "PENDING"
            assert cancel_event_after_failure.retry_count == 1
            # 模拟退避时间到达后Redis恢复，允许真实发布Worker立即补发。
            for event in db.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_id.in_(
                        scoped_outbox_repository.event_ids
                    ),
                    OutboxEvent.status == OutboxStatus.PENDING.value,
                )
            ).all():
                event.next_retry_at = utc_now()
            db.commit()

        # Redis恢复：Outbox发布成功后，Consumer按PostgreSQL终态删索引。
        publish_result = publisher_worker.run_once()
        consume_result = consumer_worker.run_once()
        assert publish_result.sent >= 1
        assert consume_result.acknowledged >= 1
        assert active_index.get_active_order(order_id) == {}
        assert order_id not in active_index.list_instrument_order_ids(
            integration_context.exchange_id,
            integration_context.symbol,
        )
        assert order_id not in active_index.list_account_order_ids(
            integration_context.account_id
        )
        assert order_id not in active_index.list_all_order_ids()
        assert redis_client.xpending(stream_name, group_name)["pending"] == 0

        with SessionLocal() as db:
            order = db.scalar(
                select(Order).where(Order.order_id == order_id)
            )
            account = db.scalar(
                select(Account).where(
                    Account.account_id == integration_context.account_id
                )
            )
            trade = db.scalar(
                select(Trade).where(Trade.order_id == order_id)
            )
            position = db.scalar(
                select(Position).where(
                    Position.account_id == integration_context.account_id
                )
            )
            cancel_event = db.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == order_id,
                    OutboxEvent.event_type
                    == "ORDER_PARTIALLY_CANCELLED",
                )
            )
            event_ids = db.scalars(
                select(OutboxEvent.event_id).where(
                    (
                        (OutboxEvent.aggregate_type == "ORDER")
                        & (OutboxEvent.aggregate_id == order_id)
                    )
                    | (
                        (OutboxEvent.aggregate_type == "TRADE")
                        & (OutboxEvent.aggregate_id == trade.trade_id)
                    )
                )
            ).all()
            assert order.traded_volume == 3
            assert order.cancelled_volume == 7
            assert order.remaining_volume == 0
            assert trade.trade_volume == 3
            assert position.total_volume == 3
            assert account.frozen_margin == Decimal("0.000000")
            assert account.frozen_commission == Decimal("0.000000")
            assert cancel_event.status == "SENT"
    finally:
        pipeline = redis_client.pipeline(transaction=True)
        if order_id:
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
            pipeline.srem(ACTIVE_ORDERS_ALL_KEY, order_id)
        for event_id in event_ids:
            pipeline.delete(processed_order_event_key(event_id))
        pipeline.delete(stream_name, f"{stream_name}:dead")
        pipeline.execute()
