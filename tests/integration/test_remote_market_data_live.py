import threading

import pytest

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.market_data.remote_feed_client import (
    RemoteFeedClient,
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
    if not settings.remote_market_data_base_url.strip():
        pytest.skip("未配置真实行情服务地址")
    codes = set()
    index = ActiveOrderIndex(redis_client)
    for order_id in index.list_all_order_ids():
        detail = index.get_active_order(order_id)
        if detail.get("order_book_id"):
            codes.add(detail["order_book_id"])
    # 真实行情联调应覆盖生产订阅目标：活动订单与有效持仓的合约并集。
    codes.update(
        RealtimePnlStore(redis_client).list_active_contract_codes()
    )
    if not codes:
        pytest.skip("Redis中没有活动订单或有效持仓合约")
    # 活动索引保存项目内部代码；真实FeedHub调用必须使用行情源代码。
    # 映射在测试开始前一次性批量构建，行为与生产订阅Worker保持一致。
    with SessionLocal() as db:
        snapshot = MarketDataCodeMappingService(
            InstrumentMarketDataMappingRepository()
        ).build_snapshot(db, codes)
    return set(snapshot.source_codes)


def test_real_feed_rest_snapshot_for_active_contracts():
    codes = configured_codes()
    client = RemoteFeedClient(create_remote_sdk_client(settings))

    result = client.get_latest_ticks(codes)

    assert set(result) == codes
    assert all(item is None or isinstance(item, dict) for item in result.values())


def test_real_feed_callback_subscription_can_start_and_stop():
    code = sorted(configured_codes())[0]
    client = RemoteFeedClient(create_remote_sdk_client(settings))
    subscribed = threading.Event()
    errors = []

    def on_subscribe(_report):
        subscribed.set()

    def on_error(error):
        errors.append(str((error.get("raw") or {}).get("code") or "ERROR"))

    subscription = client.start_tick_callbacks(
        {code},
        on_quote=lambda _data, _raw: None,
        on_subscribe=on_subscribe,
        on_message=lambda _message: None,
        on_error=on_error,
    )
    try:
        assert subscribed.wait(timeout=8), "未在8秒内收到行情订阅回执"
        assert not errors
    finally:
        subscription.stop()
        subscription.join(timeout=5)
    assert not subscription.is_alive()
