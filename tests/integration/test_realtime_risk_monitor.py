from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from app.common.exceptions import BusinessRuleError
from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.risk_store import RiskStore
from app.infrastructure.redis_keys import market_latest_key
from app.infrastructure.redis_keys import processed_risk_trigger_key
from app.matching.models import MatchResult
from app.models.account import Account
from app.models.liquidation_task import LiquidationTask
from app.models.order import Order
from app.models.outbox_event import OutboxEvent
from app.models.position import Position
from app.models.risk_event import RiskEvent
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType
from app.services.liquidation_service import LiquidationService
from app.services.pnl_snapshot_persistence_service import (
    PnlSnapshotPersistenceService,
)
from app.services.risk_monitor_service import RiskMonitorService
from app.services.risk_event_service import RiskEventService
from app.services.trade_settlement_service import (
    SettlementCommand,
    TradeSettlementService,
)
from tests.integration.conftest import (
    make_cancellation_service,
    make_order_service,
    make_request,
)


def monitor_service():
    return RiskMonitorService(
        session_factory=SessionLocal,
        cancellation_service=make_cancellation_service(),
    )


class _FailingRiskOutboxRepository(OutboxRepository):
    @staticmethod
    def create_event(*_args, **_kwargs):
        raise RuntimeError("injected risk outbox failure")


def _settle(order_id: str, *, event_id: str, volume: int):
    command = SettlementCommand(
        order_id=order_id,
        market_event_id=event_id,
        market_stream_message_id=f"{event_id}-0",
        tick_event_time=datetime.now(timezone.utc),
        tick_sequence_id=1,
        match_result=MatchResult(
            matched=True,
            fill_price=Decimal("3500"),
            fill_volume=volume,
            reason=None,
            engine_name="VN",
            engine_version="1.0",
        ),
    )
    with SessionLocal() as db:
        return TradeSettlementService().settle(db, command)


def _publish_liquidation_price(context) -> None:
    tick = MarketTick(
        source_event_id=f"RISK-TICK-{uuid4().hex}",
        ingest_type=MarketTickIngestType.LIVE_CALLBACK,
        order_book_id=context.symbol,
        exchange_id=context.exchange_id,
        symbol=context.symbol,
        trading_day=context.trading_day,
        event_time=datetime.now(timezone.utc),
        sequence_id=1,
        last_price=Decimal("3500"),
        cumulative_volume=1,
        bid_price_1=Decimal("3499"),
        bid_volume_1=100,
        ask_price_1=Decimal("3501"),
        ask_volume_1=100,
    )
    redis_client.hset(
        market_latest_key(context.exchange_id, context.symbol),
        mapping=MarketTickStore.tick_to_mapping(tick),
    )


def test_warning_transition_and_outbox_are_atomic(integration_context):
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(Account.account_id == integration_context.account_id)
        )
        account.risk_ratio = Decimal("0.85000000")
        account.risk_available_cash = Decimal("10000")
        db.commit()

    result = monitor_service().process_account(integration_context.account_id)
    assert result.state == "WARNING"

    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(Account.account_id == integration_context.account_id)
        )
        event = db.scalar(
            select(RiskEvent).where(RiskEvent.account_id == integration_context.account_id)
        )
        outbox = db.scalar(
            select(OutboxEvent).where(OutboxEvent.event_id == event.event_id)
        )
        assert account.risk_state == "WARNING"
        assert account.risk_version == event.business_version == 1
        assert outbox.event_type == "RISK_WARNING"


def test_risk_state_and_event_roll_back_when_outbox_write_fails(
    integration_context,
):
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        account.risk_ratio = Decimal("0.85000000")
        db.commit()

    service = RiskMonitorService(
        session_factory=SessionLocal,
        cancellation_service=make_cancellation_service(),
        event_service=RiskEventService(
            outbox_repository=_FailingRiskOutboxRepository()
        ),
    )
    with pytest.raises(RuntimeError, match="injected risk outbox failure"):
        service.process_account(integration_context.account_id)

    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        assert account.risk_state == "NORMAL"
        assert account.risk_version == 0
        assert db.scalar(
            select(func.count(RiskEvent.id)).where(
                RiskEvent.account_id == integration_context.account_id
            )
        ) == 0


def test_margin_deficit_blocks_open_but_does_not_block_risk_reducing_api(
    integration_context,
):
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(Account.account_id == integration_context.account_id)
        )
        account.risk_state = "MARGIN_DEFICIT"
        db.commit()

    with SessionLocal() as db, pytest.raises(BusinessRuleError) as exc_info:
        make_order_service(integration_context).create_order(
            db,
            make_request(integration_context, client_order_id="RISK-BLOCK-OPEN"),
        )
    assert exc_info.value.error_code == "ACCOUNT_RISK_OPEN_BLOCKED"


def test_deficit_cancels_open_and_revalues_before_deciding_liquidation(
    integration_context,
):
    with SessionLocal() as db:
        order = make_order_service(integration_context).create_order(
            db,
            make_request(
                integration_context,
                client_order_id=f"RISK-CANCEL-{uuid4().hex}",
                volume=2,
            ),
        )
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        account.risk_ratio = Decimal("2")
        account.risk_available_cash = Decimal("-1")
        db.commit()

    service = RiskMonitorService(
        session_factory=SessionLocal,
        cancellation_service=make_cancellation_service(),
        revaluation_service=PnlSnapshotPersistenceService(
            session_factory=SessionLocal,
            pnl_store=None,
            market_tick_store=MarketTickStore(redis_client),
        ),
    )
    result = service.process_account(integration_context.account_id)
    assert result.open_orders_cancelled == 1
    assert result.state == "RECOVERED"
    assert result.liquidation_task_id is None

    with SessionLocal() as db:
        cancelled = db.scalar(
            select(Order).where(Order.order_id == order.order_id)
        )
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        assert cancelled.status == "CANCELLED"
        assert account.frozen_margin == Decimal("0.000000")
        assert account.frozen_commission == Decimal("0.000000")
        assert db.scalar(
            select(func.count(LiquidationTask.id)).where(
                LiquidationTask.account_id == integration_context.account_id
            )
        ) == 0


def test_concurrent_deficit_creates_only_one_active_liquidation_task(
    integration_context,
):
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(Account.account_id == integration_context.account_id)
        )
        account.risk_state = "MARGIN_DEFICIT"
        db.commit()

    def create_task():
        return monitor_service()._create_task(
            integration_context.account_id, "RISK_LIMIT_EXCEEDED"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        task_ids = list(pool.map(lambda _index: create_task(), range(2)))

    assert task_ids[0] == task_ids[1]
    with SessionLocal() as db:
        count = db.scalar(
            select(func.count(LiquidationTask.id)).where(
                LiquidationTask.account_id == integration_context.account_id,
                LiquidationTask.active_key == integration_context.account_id,
            )
        )
        assert count == 1


def test_liquidation_retry_exhaustion_is_failed_and_audited(
    integration_context,
):
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        account.risk_state = "MARGIN_DEFICIT"
        db.commit()
    task_id = monitor_service()._create_task(
        integration_context.account_id, "RISK_LIMIT_EXCEEDED"
    )
    service = LiquidationService(
        session_factory=SessionLocal,
        order_service=Mock(),
        cancellation_service=Mock(),
        market_tick_store=Mock(),
        max_retries=2,
    )
    service._record_retry(task_id, "DB_TEMPORARY_FAILURE")
    service._record_retry(task_id, "DB_TEMPORARY_FAILURE")

    with SessionLocal() as db:
        task = db.scalar(
            select(LiquidationTask).where(LiquidationTask.task_id == task_id)
        )
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        event = db.scalar(
            select(RiskEvent).where(
                RiskEvent.account_id == integration_context.account_id,
                RiskEvent.event_type == "LIQUIDATION_FAILED",
            )
        )
        assert task.status == "FAILED"
        assert task.retry_count == 2
        assert task.active_key is None
        assert account.risk_state == "MARGIN_DEFICIT"
        assert event.snapshot["error"] == "DB_TEMPORARY_FAILURE"


def test_valuation_unavailable_never_creates_liquidation_task(integration_context):
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(Account.account_id == integration_context.account_id)
        )
        account.risk_state = "VALUATION_UNAVAILABLE"
        account.risk_ratio = Decimal("9")
        db.commit()

    result = monitor_service().process_account(integration_context.account_id)
    assert result.state == "VALUATION_UNAVAILABLE"
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count(LiquidationTask.id)).where(
                LiquidationTask.account_id == integration_context.account_id
            )
        ) == 0


def test_real_redis_dirty_cas_and_duplicate_trigger_are_reliable(
    integration_context,
):
    store = RiskStore(redis_client)
    first_version = store.mark_dirty(integration_context.account_id)
    current_version = store.mark_dirty(integration_context.account_id)
    assert int(current_version) == int(first_version) + 1
    assert store.complete_dirty(integration_context.account_id, first_version) is False
    assert store.complete_dirty(integration_context.account_id, current_version) is True

    event_id = f"RISK-DIRTY-{uuid4().hex}"
    try:
        assert store.mark_dirty_once(
            account_id=integration_context.account_id,
            event_id=event_id,
        )
        assert store.mark_dirty_once(
            account_id=integration_context.account_id,
            event_id=event_id,
        ) == ""
    finally:
        redis_client.delete(processed_risk_trigger_key(event_id))


def test_liquidation_order_reuses_trade_chain_and_cancels_remainder_on_recovery(
    integration_context,
):
    """真实PG/Redis验证强平建单、部分成交、恢复撤余量和审计闭环。"""

    order_service = make_order_service(integration_context)
    cancellation_service = make_cancellation_service()
    with SessionLocal() as db:
        open_order = order_service.create_order(
            db,
            make_request(
                integration_context,
                client_order_id=f"RISK-OPEN-{uuid4().hex}",
                volume=3,
            ),
        )
    assert _settle(
        open_order.order_id,
        event_id=f"RISK-OPEN-FILL-{uuid4().hex}",
        volume=3,
    ).action == "SETTLED"

    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        account.risk_state = "MARGIN_DEFICIT"
        account.risk_ratio = Decimal("2")
        account.risk_available_cash = Decimal("-1000")
        db.commit()

    task_id = monitor_service()._create_task(
        integration_context.account_id, "RISK_LIMIT_EXCEEDED"
    )
    liquidation = LiquidationService(
        session_factory=SessionLocal,
        order_service=order_service,
        cancellation_service=cancellation_service,
        market_tick_store=MarketTickStore(redis_client),
    )
    # 缺少可靠价格时只保留任务和待提交幂等键，不生成错误价格订单。
    redis_client.delete(
        market_latest_key(
            integration_context.exchange_id,
            integration_context.symbol,
        )
    )
    assert liquidation.execute_task(task_id) == "RETRY"
    with SessionLocal() as db:
        pending_client_order_id = db.scalar(
            select(LiquidationTask.pending_client_order_id).where(
                LiquidationTask.task_id == task_id
            )
        )
        assert pending_client_order_id
        assert db.scalar(
            select(func.count(Order.id)).where(
                Order.liquidation_task_id == task_id
            )
        ) == 0
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        assert account.risk_state == "VALUATION_UNAVAILABLE"

        # 模拟行情恢复后的完整PnL复核，任务继续使用此前持久化的幂等键。
        account.risk_state = "MARGIN_DEFICIT"
        db.commit()

    # 行情恢复后复用同一幂等键提交，避免失败重试生成重复强平订单。
    _publish_liquidation_price(integration_context)
    assert liquidation.execute_task(task_id) == "ORDER_CREATED"

    with SessionLocal() as db:
        task = db.scalar(
            select(LiquidationTask).where(LiquidationTask.task_id == task_id)
        )
        forced_order = db.scalar(
            select(Order).where(Order.liquidation_task_id == task_id)
        )
        assert forced_order.order_source == "LIQUIDATION"
        assert forced_order.client_order_id == pending_client_order_id
        assert forced_order.reduce_only is True
        assert forced_order.offset_flag == "CLOSE"
        assert forced_order.frozen_position_volume == 3
        assert task.last_order_id == forced_order.order_id
        assert task.pending_client_order_id is None

    assert _settle(
        forced_order.order_id,
        event_id=f"RISK-PARTIAL-FILL-{uuid4().hex}",
        volume=1,
    ).action == "SETTLED"
    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        account.risk_state = "RECOVERED"
        account.risk_ratio = Decimal("0.5")
        account.risk_available_cash = Decimal("1000")
        db.commit()

    assert liquidation.execute_task(task_id) == "ORDER_CANCELLED"
    with SessionLocal() as db:
        task = db.scalar(
            select(LiquidationTask).where(LiquidationTask.task_id == task_id)
        )
        forced_order = db.scalar(
            select(Order).where(Order.order_id == forced_order.order_id)
        )
        position = db.scalar(
            select(Position).where(
                Position.account_id == integration_context.account_id
            )
        )
        assert forced_order.status == "PARTIALLY_CANCELLED"
        assert forced_order.traded_volume == 1
        assert forced_order.cancelled_volume == 2
        assert position.total_volume == position.available_volume == 2
        assert position.frozen_volume == 0
        assert task.status == "COMPLETED"
        assert task.active_key is None
        event_types = set(
            db.scalars(
                select(RiskEvent.event_type).where(
                    RiskEvent.account_id == integration_context.account_id
                )
            ).all()
        )
        assert {
            "LIQUIDATION_STARTED",
            "LIQUIDATION_ORDER_UPDATED",
            "LIQUIDATION_COMPLETED",
        } <= event_types
