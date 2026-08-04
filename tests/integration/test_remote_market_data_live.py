import threading

import pytest

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.market_data.remote_feed_client import (
    RemoteFeedClient,
    RemoteMarketDataConfigurationError,
    RemoteMarketDataSdkUnavailableError,
    create_remote_sdk_client,
)
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.repositories.instrument_market_data_mapping_repository import (
    InstrumentMarketDataMappingRepository,
)
from app.services.market_data_code_mapping_service import (
    MarketDataCodeMappingService,
)


pytestmark = pytest.mark.integration


def configured_codes():
    codes = set()
    index = ActiveOrderIndex(redis_client)
    for order_id in index.list_all_order_ids():
        detail = index.get_active_order(order_id)
        if detail.get("order_book_id"):
            codes.add(detail["order_book_id"])
    codes.update(RealtimePnlStore(redis_client).list_active_contract_codes())
    if not codes:
        pytest.skip("Redis中没有活动订单或有效持仓合约")
    with SessionLocal() as db:
        snapshot = MarketDataCodeMappingService(
            InstrumentMarketDataMappingRepository()
        ).build_snapshot(db, codes)
    return set(snapshot.source_codes)


def create_real_client_or_skip() -> RemoteFeedClient:
    try:
        sdk_client = create_remote_sdk_client(settings)
    except RemoteMarketDataSdkUnavailableError as exc:
        pytest.skip(str(exc))
    except RemoteMarketDataConfigurationError as exc:
        pytest.skip(str(exc))
    return RemoteFeedClient(sdk_client)


def test_real_ymm_live_data_connects_subscribes_and_closes():
    """无论是否开盘，真实SDK都必须完成鉴权、订阅并正常关闭线程。"""

    code = sorted(configured_codes())[0]
    client = create_real_client_or_skip()
    subscribed = threading.Event()
    errors = []

    subscription = client.start_tick_callbacks(
        {code},
        on_quote=lambda _data, _raw: None,
        on_subscribe=lambda _report: subscribed.set(),
        on_message=lambda _message: None,
        on_error=lambda error: errors.append(
            str((error.get("raw") or {}).get("code") or "ERROR")
        ),
    )
    try:
        assert subscribed.wait(timeout=5), "未收到本地订阅确认"
        assert f"tick_{code}" in client.sdk_client.subscriptions
        status = client.sdk_client.get_status()
        assert status.hub == "connected"
        assert status.catalog == "ready"
        assert not errors
    finally:
        subscription.stop()
        subscription.join(timeout=5)
    assert not subscription.is_alive()


def test_real_ymm_live_data_receives_tick_when_source_is_publishing():
    """交易时段必须收到真实Tick；中心尚无行情时只跳过Tick专项验收。"""

    code = sorted(configured_codes())[0]
    client = create_real_client_or_skip()
    subscribed = threading.Event()
    tick_received = threading.Event()
    received = []

    def on_quote(data, _raw):
        received.append(data)
        tick_received.set()

    subscription = client.start_tick_callbacks(
        {code},
        on_quote=on_quote,
        on_subscribe=lambda _report: subscribed.set(),
        on_message=lambda _message: None,
        on_error=lambda _error: None,
    )
    try:
        assert subscribed.wait(timeout=5), "未收到本地订阅确认"
        if not tick_received.wait(timeout=20):
            status = client.sdk_client.get_status()
            assert status.hub == "connected"
            assert status.catalog == "ready"
            assert f"tick_{code}" in client.sdk_client.subscriptions
            if status.rqdata != "connected":
                pytest.skip(
                    "行情中心上游当前不可用，"
                    f"rqdata={status.rqdata}，等待恢复后验收Tick"
                )
            if status.last_market_data_at is None:
                pytest.skip("行情中心当前尚未产生实时行情，等待交易时段验收Tick")
            pytest.fail("行情中心正在产生行情，但订阅合约20秒内未收到Tick")

        tick = received[-1]
        assert tick["action"] == "feed"
        assert tick["channel"] == f"tick_{code}"
        assert tick["order_book_id"] == code
        assert tick.get("trading_date") is not None
        assert tick.get("datetime") is not None
    finally:
        subscription.stop()
        subscription.join(timeout=5)
    assert not subscription.is_alive()
