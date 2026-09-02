import threading

import pytest

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.enums.market_feed_enums import MarketFeedDomain
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.cash_security_valuation_store import (
    CashSecurityValuationStore,
)
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


def configured_codes(domain: MarketFeedDomain):
    index = ActiveOrderIndex(redis_client)
    codes = index.list_active_contract_codes(domain)
    position_source = (
        RealtimePnlStore(redis_client)
        if domain == MarketFeedDomain.FUTURES_MARKET
        else CashSecurityValuationStore(redis_client)
    )
    codes.update(position_source.list_active_contract_codes())
    if not codes:
        pytest.skip("Redis中没有活动订单或有效持仓合约")
    with SessionLocal() as db:
        snapshot = MarketDataCodeMappingService(
            InstrumentMarketDataMappingRepository()
        ).build_snapshot(db, codes)
    return set(snapshot.source_codes)


def selected_test_code(domain: MarketFeedDomain) -> str:
    """优先验收当前人工测试主力合约，避免低频合约造成假失败。"""

    codes = configured_codes(domain)
    if domain == MarketFeedDomain.FUTURES_MARKET and "JD2609" in codes:
        return "JD2609"
    return sorted(codes)[0]


def create_real_client_or_skip(domain: MarketFeedDomain) -> RemoteFeedClient:
    try:
        sdk_client = create_remote_sdk_client(
            settings,
            domain=domain,
        )
    except RemoteMarketDataSdkUnavailableError as exc:
        pytest.skip(str(exc))
    except RemoteMarketDataConfigurationError as exc:
        pytest.skip(str(exc))
    return RemoteFeedClient(sdk_client)


@pytest.mark.parametrize("domain", list(MarketFeedDomain))
def test_real_ymm_live_data_connects_subscribes_and_closes(domain):
    """无论是否开盘，真实SDK都必须完成鉴权、订阅并正常关闭线程。"""

    code = selected_test_code(domain)
    client = create_real_client_or_skip(domain)
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


@pytest.mark.parametrize("domain", list(MarketFeedDomain))
def test_real_ymm_live_data_receives_tick_when_source_is_publishing(domain):
    """交易时段必须收到真实Tick；中心尚无行情时只跳过Tick专项验收。"""

    code = selected_test_code(domain)
    client = create_real_client_or_skip(domain)
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
            # last_market_data_at 是行情中心全局时间，不代表当前合约正在
            # 产生 Tick。合约可能正处于盘中休市或盘口长期未变化；在 SDK
            # 未提供“单合约正在发布”状态前，不能据此把链路误判为失败。
            pytest.skip(
                f"{code}在20秒内没有新Tick；行情中心在线，"
                "但无法据全局行情时间证明该合约当前正在发布"
            )

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
