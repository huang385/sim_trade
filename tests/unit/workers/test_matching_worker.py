import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.core.config import settings
from app.enums.market_feed_enums import MarketFeedDomain
from app.services.market_tick_matching_service import (
    UnsupportedMarketTickEventError,
)
from app.workers.matching_worker import (
    MatchingWorker,
    build_arrival_worker,
    build_matching_worker,
)


FIELDS = {
    "event_id": "TICK-1",
    "event_type": "MARKET_TICK",
    "exchange_id": "SHFE",
    "symbol": "AG2609",
    "payload": "{}",
}


def make_worker(*, side_effect=None, failure_count=1):
    consumer = Mock()
    consumer.consumer_name = "matching-1"
    consumer.increment_failure.return_value = failure_count
    consumer.claim_stale_messages.return_value = []
    consumer.read_new_messages.return_value = []
    service = Mock()
    if side_effect is None:
        service.process.return_value = SimpleNamespace(
            candidate_count=1,
            matched_count=1,
            settled_count=1,
            idempotent_count=0,
        )
    else:
        service.process.side_effect = side_effect
    worker = MatchingWorker(
        stream_consumer=consumer,
        matching_service=service,
        batch_size=10,
        block_ms=1,
        pending_idle_ms=60000,
        max_retries=10,
        retry_interval_seconds=0,
    )
    return worker, consumer, service


def test_success_acknowledges_and_database_failure_does_not_ack():
    worker, consumer, _ = make_worker()
    assert worker.handle_message("1-0", FIELDS) == "acknowledged"
    consumer.acknowledge.assert_called_once_with("1-0")

    worker, consumer, _ = make_worker(side_effect=RuntimeError("postgres down"))
    assert worker.handle_message("2-0", FIELDS) == "retry"
    consumer.acknowledge.assert_not_called()


def test_new_settlement_logs_info(caplog):
    """本轮产生新成交时应保留INFO日志，方便交易监控。"""

    worker, _, _ = make_worker()

    with caplog.at_level(
        logging.DEBUG,
        logger="app.workers.matching_worker",
    ):
        result = worker.handle_message("1-0", FIELDS)

    assert result == "acknowledged"
    assert any(
        record.levelno == logging.INFO
        and "行情撮合产生成交" in record.getMessage()
        for record in caplog.records
    )


def test_no_match_logs_debug_without_info(caplog):
    """普通未成交Tick只记录DEBUG，默认INFO级别下不会刷屏。"""

    worker, _, service = make_worker()
    service.process.return_value = SimpleNamespace(
        candidate_count=3,
        matched_count=0,
        settled_count=0,
        idempotent_count=0,
    )

    with caplog.at_level(
        logging.DEBUG,
        logger="app.workers.matching_worker",
    ):
        result = worker.handle_message("2-0", FIELDS)

    assert result == "acknowledged"
    assert any(
        record.levelno == logging.DEBUG
        and "未产生新成交" in record.getMessage()
        for record in caplog.records
    )
    assert not any(
        record.levelno == logging.INFO
        and "行情撮合" in record.getMessage()
        for record in caplog.records
    )


def test_idempotent_replay_without_new_settlement_logs_debug(caplog):
    """幂等重放没有新增成交时不得重复打印INFO成交日志。"""

    worker, _, service = make_worker()
    service.process.return_value = SimpleNamespace(
        candidate_count=1,
        matched_count=1,
        settled_count=0,
        idempotent_count=1,
    )

    with caplog.at_level(
        logging.DEBUG,
        logger="app.workers.matching_worker",
    ):
        result = worker.handle_message("3-0", FIELDS)

    assert result == "acknowledged"
    assert any(
        record.levelno == logging.DEBUG
        and "idempotent=1" in record.getMessage()
        for record in caplog.records
    )
    assert not any(
        record.levelno == logging.INFO
        and "行情撮合产生成交" in record.getMessage()
        for record in caplog.records
    )


def test_database_failure_logs_warning_and_does_not_ack(caplog):
    """暂时性数据库异常继续保留WARNING并让原消息留在Pending。"""

    worker, consumer, _ = make_worker(
        side_effect=RuntimeError("postgres down")
    )

    with caplog.at_level(
        logging.WARNING,
        logger="app.workers.matching_worker",
    ):
        result = worker.handle_message("4-0", FIELDS)

    assert result == "retry"
    consumer.acknowledge.assert_not_called()
    assert any(
        record.levelno == logging.WARNING
        and "保留Pending" in record.getMessage()
        for record in caplog.records
    )


def test_unknown_event_goes_to_dead_letter_before_ack():
    worker, consumer, _ = make_worker(
        side_effect=UnsupportedMarketTickEventError("unsupported")
    )
    assert worker.handle_message("1-0", FIELDS) == "dead_lettered"
    consumer.publish_dead_letter.assert_called_once()
    consumer.acknowledge.assert_called_once_with("1-0")


def test_max_retry_dead_letter_failure_keeps_original_pending():
    worker, consumer, _ = make_worker(
        side_effect=RuntimeError("postgres down"), failure_count=10
    )
    consumer.publish_dead_letter.side_effect = ConnectionError("redis down")
    assert worker.handle_message("1-0", FIELDS) == "retry"
    consumer.acknowledge.assert_not_called()


def test_pending_and_new_messages_are_both_processed_and_stop_is_graceful():
    worker, consumer, _ = make_worker()
    consumer.claim_stale_messages.return_value = [("1-0", FIELDS)]
    consumer.read_new_messages.return_value = [("2-0", FIELDS)]
    result = worker.run_once()
    assert result.received == 2
    assert result.acknowledged == 2
    worker.request_stop()
    assert worker.stop_event.is_set()


def test_deleted_pending_tombstone_is_acknowledged_without_matching():
    worker, consumer, service = make_worker()
    assert worker.handle_message("1-0", None) == "acknowledged"
    service.process.assert_not_called()
    consumer.acknowledge.assert_called_once_with("1-0")


def test_worker_is_built_with_engine_created_by_registry():
    """Worker 启动时只通过 Registry 创建一次引擎并注入编排服务。"""

    fake_engine = Mock()

    with patch(
        "app.workers.matching_worker.create_matching_engine",
        return_value=fake_engine,
    ) as create_engine:
        worker = build_matching_worker(MarketFeedDomain.FUTURES_MARKET)

    create_engine.assert_called_once_with(settings.matching_engine_name)
    assert worker.matching_service.matching_engine is fake_engine


def test_securities_worker_uses_independent_stream_without_derivative_engine():
    with patch(
        "app.workers.matching_worker.create_matching_engine"
    ) as create_engine:
        worker = build_matching_worker(MarketFeedDomain.SECURITIES_MARKET)

    create_engine.assert_not_called()
    assert worker.stream_consumer.stream_name == (
        settings.securities_market_tick_stream_name
    )
    assert worker.stream_consumer.group_name == (
        settings.securities_matching_consumer_group
    )
    assert worker.matching_service.enabled is settings.stock_matching_enabled


def test_securities_worker_gates_etf_matching_independently():
    with (
        patch.object(settings, "stock_matching_enabled", False),
        patch.object(settings, "etf_matching_enabled", True),
    ):
        worker = build_matching_worker(MarketFeedDomain.SECURITIES_MARKET)

    assert worker.matching_service.enabled is True
    assert worker.matching_service.instrument_types == frozenset({"ETF"})


def test_stock_matching_flag_does_not_implicitly_enable_etf():
    with (
        patch.object(settings, "stock_matching_enabled", True),
        patch.object(settings, "etf_matching_enabled", False),
    ):
        worker = build_matching_worker(MarketFeedDomain.SECURITIES_MARKET)

    assert worker.matching_service.instrument_types == frozenset(
        {"STOCK", "CONVERTIBLE_BOND"}
    )


def test_arrival_consumers_have_independent_groups_and_failure_namespaces():
    futures = build_arrival_worker(MarketFeedDomain.FUTURES_MARKET, Mock())
    securities = build_arrival_worker(
        MarketFeedDomain.SECURITIES_MARKET, Mock()
    )

    assert futures.stream_consumer.group_name != (
        securities.stream_consumer.group_name
    )
    assert futures.stream_consumer.failure_key_factory("1-0") != (
        securities.stream_consumer.failure_key_factory("1-0")
    )
