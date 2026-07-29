import json
from uuid import uuid4

import pytest
from redis.exceptions import RedisError
from sqlalchemy import select

import app.infrastructure.realtime_pnl_store as pnl_store_module
from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.market_tick_stream_consumer import (
    MarketTickStreamConsumer,
)
from app.infrastructure.order_stream_consumer import OrderStreamConsumer
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.infrastructure.redis_keys import (
    pnl_account_key,
    processed_pnl_fact_event_key,
)
from app.models.account import Account
from app.models.outbox_event import OutboxEvent
from app.schemas.order_schema import OrderCancelRequest
from app.services.active_position_cache import ActivePositionCache
from app.services.realtime_pnl_service import RealtimePnlService
from app.services.trade_created_pnl_service import TradeCreatedPnlService
from app.workers.realtime_pnl_worker import RealtimePnlWorker
from app.workers.trade_event_pnl_worker import TradeEventPnlWorker
from tests.integration.conftest import (
    make_cancellation_service,
    make_order_service,
    make_request,
)


def _publish_outbox_event(stream_name: str, event: OutboxEvent) -> str:
    """把已提交Outbox快照投递到测试专属Stream。"""

    return redis_client.xadd(
        stream_name,
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "payload": json.dumps(event.payload),
        },
    )


def test_freeze_and_cancel_refresh_account_snapshot_without_market_tick(
    integration_context,
    monkeypatch,
):
    """
    完整验证：订单事务→Outbox→事实Worker ACK→账户Dirty→PnL Worker
    Redis写入→CAS清理；随后撤单在没有任何新Tick时再次刷新账户快照。
    """

    try:
        redis_client.ping()
    except RedisError as exc:
        pytest.skip(f"Redis不可连接: {exc}")

    suffix = uuid4().hex[:10].upper()
    dirty_key = f"test:pnl:dirty_account_facts:{suffix}"
    version_key = f"test:pnl:dirty_account_versions:{suffix}"
    order_stream = f"test:stream:account-facts:{suffix}"
    order_group = f"test:group:account-facts:{suffix}"
    order_dead = f"test:stream:account-facts:dead:{suffix}"
    market_stream = f"test:stream:pnl-empty:{suffix}"
    market_group = f"test:group:pnl-empty:{suffix}"
    market_dead = f"test:stream:pnl-empty:dead:{suffix}"
    lease_key = f"test:pnl:lease:{suffix}"
    monkeypatch.setattr(
        pnl_store_module,
        "PNL_DIRTY_ACCOUNT_FACTS_KEY",
        dirty_key,
    )
    monkeypatch.setattr(
        pnl_store_module,
        "PNL_DIRTY_ACCOUNT_FACT_VERSIONS_KEY",
        version_key,
    )

    store = RealtimePnlStore(
        redis_client,
        worker_lease_key=lease_key,
    )
    fact_consumer = OrderStreamConsumer(
        redis_client,
        stream_name=order_stream,
        group_name=order_group,
        consumer_name=f"fact-{suffix}",
        dead_letter_stream=order_dead,
        failure_ttl_seconds=60,
    )
    fact_consumer.ensure_group()
    fact_worker = TradeEventPnlWorker(
        stream_consumer=fact_consumer,
        service=TradeCreatedPnlService(
            pnl_store=store,
            processed_ttl_seconds=60,
        ),
    )
    pnl_consumer = MarketTickStreamConsumer(
        redis_client,
        stream_name=market_stream,
        group_name=market_group,
        consumer_name=f"pnl-{suffix}",
        dead_letter_stream=market_dead,
        failure_ttl_seconds=60,
    )
    pnl_consumer.ensure_group()
    pnl_worker = RealtimePnlWorker(
        stream_consumer=pnl_consumer,
        service=RealtimePnlService(
            active_position_cache=ActivePositionCache(
                session_factory=SessionLocal,
                refresh_ms=60_000,
            ),
            pnl_store=store,
        ),
        pnl_store=store,
        batch_size=100,
        block_ms=1,
        pending_idle_ms=60_000,
        max_retries=3,
        retry_interval_seconds=0,
        calculation_interval_ms=500,
        lease_owner=f"pnl-{suffix}",
    )
    # 本测试只验证账户事实增量刷新，不重建系统级活动持仓Redis索引。
    pnl_worker._indexes_rebuilt = True
    pnl_worker._next_reconciliation_at = float("inf")

    processed_keys: list[str] = []
    try:
        with SessionLocal() as db:
            order = make_order_service(
                integration_context
            ).create_order(
                db,
                make_request(
                    integration_context,
                    client_order_id=f"STATE-{suffix}",
                    volume=2,
                ),
            )
            accepted_event = db.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == order.order_id,
                    OutboxEvent.event_type == "ORDER_ACCEPTED",
                )
            )
            assert accepted_event is not None

        processed_keys.append(
            processed_pnl_fact_event_key(
                accepted_event.event_id
            )
        )
        _publish_outbox_event(order_stream, accepted_event)
        fact_worker.run_once()
        pending = redis_client.xpending(order_stream, order_group)
        assert pending["pending"] == 0
        first_version = redis_client.hget(
            version_key,
            integration_context.account_id,
        )
        assert first_version is not None

        pnl_worker.run_once(force_flush=True)
        frozen_snapshot = store.get_account(
            integration_context.account_id
        )
        assert frozen_snapshot["available_cash"] == "91594.000000"
        with SessionLocal() as db:
            frozen_account = db.scalar(
                select(Account).where(
                    Account.account_id
                    == integration_context.account_id
                )
            )
            assert str(frozen_account.frozen_margin) == "8400.000000"
            assert str(frozen_account.frozen_commission) == "6.000000"
        assert not redis_client.sismember(
            dirty_key,
            integration_context.account_id,
        )
        assert redis_client.hget(
            version_key,
            integration_context.account_id,
        ) == first_version

        with SessionLocal() as db:
            make_cancellation_service().cancel_order(
                db=db,
                order_id=order.order_id,
                request=OrderCancelRequest(
                    account_id=integration_context.account_id
                ),
            )
            cancelled_event = db.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == order.order_id,
                    OutboxEvent.event_type == "ORDER_CANCELLED",
                )
            )
            assert cancelled_event is not None

        processed_keys.append(
            processed_pnl_fact_event_key(
                cancelled_event.event_id
            )
        )
        _publish_outbox_event(order_stream, cancelled_event)
        fact_worker.run_once()
        second_version = redis_client.hget(
            version_key,
            integration_context.account_id,
        )
        assert int(second_version) > int(first_version)

        # 全程没有发布行情Tick，仍必须仅凭账户事实V2重新加载冻结字段。
        pnl_worker.run_once(force_flush=True)
        released_snapshot = store.get_account(
            integration_context.account_id
        )
        assert released_snapshot["available_cash"] == "100000.000000"
        assert redis_client.hget(
            version_key,
            integration_context.account_id,
        ) == second_version
        assert not redis_client.sismember(
            dirty_key,
            integration_context.account_id,
        )

        with SessionLocal() as db:
            account = db.scalar(
                select(Account).where(
                    Account.account_id
                    == integration_context.account_id
                )
            )
            assert released_snapshot["available_cash"] == str(
                account.available_cash
            )
    finally:
        store.release_worker_lease(pnl_worker.lease_owner)
        redis_client.delete(
            dirty_key,
            version_key,
            order_stream,
            order_dead,
            market_stream,
            market_dead,
            lease_key,
            pnl_account_key(integration_context.account_id),
            *processed_keys,
        )
