import json

from app.services.trade_created_pnl_service import TradeCreatedPnlService
from app.workers.trade_event_pnl_worker import TradeEventPnlWorker


class IdempotentFactStore:
    """用最小内存实现模拟Redis的事件去重、版本递增和Dirty保留。"""

    def __init__(self):
        self.processed_event_ids: set[str] = set()
        self.current_version = 0
        self.dirty_version: str | None = None
        self.mark_calls = 0

    def mark_account_fact_dirty_once(
        self,
        *,
        event_id,
        account_id,
        processed_ttl_seconds,
    ):
        _ = account_id, processed_ttl_seconds
        self.mark_calls += 1
        if event_id in self.processed_event_ids:
            return None
        self.processed_event_ids.add(event_id)
        self.current_version += 1
        self.dirty_version = str(self.current_version)
        return self.dirty_version


class AckFailsOnceConsumer:
    """第一次ACK模拟Redis故障，第二次重投时恢复正常。"""

    def __init__(self):
        self.ack_calls = 0
        self.failure_count = 0
        self.cleared = []

    def acknowledge(self, _message_id):
        self.ack_calls += 1
        if self.ack_calls == 1:
            raise RuntimeError("ack unavailable")
        return 1

    def increment_failure(self, _message_id):
        self.failure_count += 1
        return self.failure_count

    def clear_failure(self, message_id):
        self.cleared.append(message_id)

    def publish_dead_letter(self, **_kwargs):
        raise AssertionError("可重试ACK故障不应进入死信")


def test_ack_failure_redelivery_keeps_fact_event_idempotent():
    """
    业务Dirty已写入但ACK失败时，旧Pending会被再次消费。

    第二次处理必须命中event_id幂等键：不能再次递增账户版本，但仍要完成
    ACK并保留第一次产生的Dirty，供RealtimePnlWorker继续刷新。
    """

    store = IdempotentFactStore()
    consumer = AckFailsOnceConsumer()
    worker = TradeEventPnlWorker(
        stream_consumer=consumer,
        service=TradeCreatedPnlService(
            pnl_store=store,
            processed_ttl_seconds=60,
        ),
    )
    fields = {
        "event_id": "EVENT-ACK-001",
        "event_type": "ORDER_ACCEPTED",
        "payload": json.dumps(
            {
                "event_id": "EVENT-ACK-001",
                "account_id": "A001",
                "exchange_id": "DCE",
                "symbol": "JD2609",
            }
        ),
    }

    assert worker.handle_message("1-0", fields) == "retry"
    assert store.current_version == 1
    assert store.dirty_version == "1"

    assert worker.handle_message("1-0", fields) == "acknowledged"
    assert store.mark_calls == 2
    assert store.current_version == 1
    assert store.dirty_version == "1"
    assert consumer.ack_calls == 2
    assert consumer.cleared == ["1-0"]
