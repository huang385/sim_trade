from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.market_tick_stream_consumer import (
    MarketTickStreamConsumer,
)
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.infrastructure.redis_keys import (
    PNL_DIRTY_ACCOUNTS_KEY,
    PNL_DIRTY_POSITIONS_KEY,
    PNL_DIRTY_POSITION_VERSIONS_KEY,
    market_latest_key,
    pnl_account_key,
    pnl_account_positions_key,
    pnl_contract_positions_key,
    pnl_position_key,
)
from app.main import app
from app.models.account import Account
from app.models.position import Position
from app.models.position_detail import PositionDetail
from app.schemas.market_tick_schema import (
    MarketTick,
    MarketTickIngestType,
)
from app.services.active_position_cache import ActivePositionCache
from app.services.pnl_snapshot_persistence_service import (
    PnlSnapshotPersistenceService,
)
from app.services.realtime_pnl_service import RealtimePnlService
from app.workers.realtime_pnl_worker import RealtimePnlWorker


pytestmark = pytest.mark.integration


def test_realtime_tick_redis_api_and_postgres_persistence(
    integration_context,
):
    """
    使用真实PostgreSQL和Redis验证：
    Tick只先写实时快照与Dirty，随后批量持久化并支持API回退。
    """

    try:
        redis_client.ping()
    except Exception as exc:
        pytest.skip(f"Redis不可连接: {exc}")

    suffix = uuid4().hex[:10]
    position_id = f"PNL-P-{suffix}"
    detail_id = f"PNL-PD-{suffix}"
    stream_name = f"stream:it:pnl:{suffix}"
    group_name = f"group:it:pnl:{suffix}"
    dead_stream = f"stream:it:pnl-dead:{suffix}"
    store = RealtimePnlStore(redis_client)
    redis_keys = [
        stream_name,
        dead_stream,
        market_latest_key(
            integration_context.exchange_id,
            integration_context.symbol,
        ),
        pnl_position_key(position_id),
        pnl_account_key(integration_context.account_id),
        pnl_account_positions_key(integration_context.account_id),
        pnl_contract_positions_key(
            integration_context.exchange_id,
            integration_context.symbol,
        ),
    ]

    with SessionLocal() as db:
        account = db.scalar(
            select(Account).where(
                Account.account_id == integration_context.account_id
            )
        )
        account.used_margin = Decimal("8400")
        account.available_cash = Decimal("91600")
        db.add(
            Position(
                position_id=position_id,
                account_id=integration_context.account_id,
                order_book_id=integration_context.symbol,
                exchange_id=integration_context.exchange_id,
                symbol=integration_context.symbol,
                direction="LONG",
                total_volume=2,
                today_volume=1,
                yesterday_volume=1,
                frozen_volume=0,
                available_volume=2,
                average_open_price=Decimal("3400"),
                position_cost=Decimal("68000"),
                used_margin=Decimal("8400"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                daily_position_pnl=Decimal("0"),
                daily_close_pnl=Decimal("0"),
                trading_day=integration_context.trading_day,
            )
        )
        db.add(
            PositionDetail(
                position_detail_id=detail_id,
                position_id=position_id,
                account_id=integration_context.account_id,
                open_trade_id=f"PNL-T-{suffix}",
                order_book_id=integration_context.symbol,
                exchange_id=integration_context.exchange_id,
                symbol=integration_context.symbol,
                direction="LONG",
                open_trading_day=integration_context.trading_day,
                open_price=Decimal("3400"),
                pnl_base_price=Decimal("3500"),
                original_volume=2,
                remaining_volume=2,
                frozen_volume=0,
                open_margin=Decimal("8400"),
                remaining_margin=Decimal("8400"),
                open_commission=Decimal("6"),
                status="OPEN",
            )
        )
        db.commit()

    consumer = MarketTickStreamConsumer(
        redis_client,
        stream_name=stream_name,
        group_name=group_name,
        consumer_name=f"pnl-{suffix}",
        dead_letter_stream=dead_stream,
        failure_ttl_seconds=60,
    )
    consumer.ensure_group()
    worker = RealtimePnlWorker(
        stream_consumer=consumer,
        service=RealtimePnlService(
            active_position_cache=ActivePositionCache(
                session_factory=SessionLocal,
                refresh_ms=1000,
            ),
            pnl_store=store,
            market_tick_store=MarketTickStore(redis_client),
        ),
        pnl_store=store,
        market_tick_store=MarketTickStore(redis_client),
        batch_size=100,
        block_ms=1,
        pending_idle_ms=60000,
        max_retries=3,
        retry_interval_seconds=0,
    )
    tick_store = MarketTickStore(
        redis_client,
        stream_name=stream_name,
    )

    try:
        # run_once绕过生产run_forever的抢租约循环，测试必须先显式取得
        # 单写者租约，才能验证受租约屏障保护的实时快照写入。
        assert store.acquire_worker_lease(
            worker.lease_owner,
            worker.lease_ttl_seconds,
        )
        for sequence, price in enumerate(
            ("3517", "3518", "3519", "3520"),
            start=1,
        ):
            tick_store.publish(
                MarketTick(
                    source_event_id=(
                        f"PNL-TICK-{suffix}-{sequence}"
                    ),
                    ingest_type=MarketTickIngestType.LIVE_CALLBACK,
                    order_book_id=integration_context.symbol,
                    exchange_id=integration_context.exchange_id,
                    symbol=integration_context.symbol,
                    trading_day=integration_context.trading_day,
                    event_time=datetime.now(timezone.utc),
                    sequence_id=sequence,
                    last_price=Decimal(price),
                    cumulative_volume=sequence,
                    bid_volume_1=1,
                    ask_volume_1=1,
                )
            )
        # 生产循环按500ms自动刷新；集成测试显式结束当前窗口。
        worker.run_once(force_flush=True)

        position_snapshot = store.get_position(position_id)
        account_snapshot = store.get_account(
            integration_context.account_id
        )
        assert (
            position_snapshot["cumulative_unrealized_pnl"]
            == "2400.000000"
        )
        assert position_snapshot["daily_position_pnl"] == "400.000000"
        pending = redis_client.xpending(stream_name, group_name)
        pending_count = (
            pending["pending"]
            if isinstance(pending, dict)
            else pending[0]
        )
        assert pending_count == 0
        assert worker.stats_snapshot().ticks_coalesced == 3
        assert account_snapshot["daily_pnl"] == "400.000000"
        assert position_id in redis_client.smembers(
            PNL_DIRTY_POSITIONS_KEY
        )

        client = TestClient(app)
        realtime_response = client.get(
            f"/api/accounts/{integration_context.account_id}/pnl/realtime"
        )
        assert realtime_response.status_code == 200
        assert (
            realtime_response.json()["data_source"]
            == "REDIS_REALTIME"
        )

        result = PnlSnapshotPersistenceService(
            session_factory=SessionLocal,
            pnl_store=store,
            market_tick_store=MarketTickStore(redis_client),
        ).persist_batch(500)
        # Redis Dirty集合是系统级共享集合；本机若已有其他活动持仓，冷启动
        # 完整对账可能让同一批同时持久化多条。下面继续精确校验本测试持仓。
        assert result.positions_persisted >= 1
        assert result.accounts_persisted >= 1

        with SessionLocal() as db:
            position = db.scalar(
                select(Position).where(
                    Position.position_id == position_id
                )
            )
            account = db.scalar(
                select(Account).where(
                    Account.account_id
                    == integration_context.account_id
                )
            )
            assert position.unrealized_pnl == Decimal("2400.000000")
            assert position.daily_position_pnl == Decimal("400.000000")
            assert account.unrealized_pnl == Decimal("2400.000000")
            assert account.daily_position_pnl == Decimal("400.000000")
            assert account.equity == Decimal("102400.000000")

        redis_client.delete(
            pnl_account_key(integration_context.account_id)
        )
        fallback_response = client.get(
            f"/api/accounts/{integration_context.account_id}/pnl/realtime"
        )
        assert fallback_response.status_code == 200
        assert (
            fallback_response.json()["data_source"]
            == "POSTGRES_SNAPSHOT"
        )
        assert fallback_response.json()["daily_pnl"] == "400.000000"
    finally:
        store.release_worker_lease(worker.lease_owner)
        redis_client.delete(*redis_keys)
        redis_client.srem(PNL_DIRTY_POSITIONS_KEY, position_id)
        redis_client.hdel(
            PNL_DIRTY_POSITION_VERSIONS_KEY,
            position_id,
        )
        redis_client.srem(
            PNL_DIRTY_ACCOUNTS_KEY,
            integration_context.account_id,
        )
