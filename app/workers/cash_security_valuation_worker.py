"""将现金证券行情和业务事实路由到估值队列的消费者。"""

import json
import logging
import os
import signal
import socket
from threading import Event

from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.cash_security_valuation_store import CashSecurityValuationStore
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.market_tick_stream_consumer import MarketTickStreamConsumer
from app.infrastructure.order_stream_consumer import OrderStreamConsumer
from app.infrastructure.redis_keys import (
    CASH_VALUATION_TICK_CONSUMER_GROUP,
    SECURITIES_MARKET_TICK_STREAM,
    cash_valuation_tick_failure_key,
)
from app.services.cash_security_valuation_service import (
    CASH_TYPES,
    CashSecurityValuationService,
)


logger = logging.getLogger(__name__)
MAX_FAILURES = 10
FAILURE_TTL_SECONDS = 7 * 24 * 60 * 60


def _consumer_name(prefix: str) -> str:
    return f"{prefix}-{socket.gethostname()}-{os.getpid()}"


class CashSecurityValuationFactWorker:
    """消费已提交业务事实，只使现金证券估值状态失效或重建路由索引。"""

    def __init__(self, *, stream_consumer: OrderStreamConsumer, service: CashSecurityValuationService) -> None:
        self.stream_consumer = stream_consumer
        self.service = service
        self.stop_event = Event()

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def handle_message(self, message_id: str, fields: dict[str, str] | None) -> str:
        if fields is None:
            self.stream_consumer.acknowledge(message_id)
            self.stream_consumer.clear_failure(message_id)
            return "tombstone"
        try:
            payload = json.loads(fields.get("payload", "{}"))
            if payload.get("fact_reason") == "CASH_SECURITY_REALTIME_VALUATION":
                # 本 Worker 自己写出的账户、持仓绝对事实无需再次标脏；回灌会形成
                # “估值 -> 事件 -> 标脏 -> 再估值”的无限循环。
                action = "self_valuation_ignored"
            elif payload.get("instrument_type") not in CASH_TYPES and payload.get("account_type") not in {"SECURITIES_CASH", "STOCK"}:
                action = "ignored"
            else:
                # 持仓变化会改变“行情 -> 账户”的可重建路由索引。Outbox 事件已提交，
                # 此处再读取数据库，确保索引只基于权威的最终持仓状态。
                if payload.get("event_type") in {"POSITION_UPDATED", "POSITION_CLOSED"}:
                    position_id = payload.get("position_id") or payload.get("entity_id")
                    if position_id:
                        self.service.refresh_active_index_for_position(position_id)
                account_id = payload.get("account_id")
                if account_id:
                    self.service.mark_account_dirty(account_id=account_id, source_event_id=payload.get("event_id", message_id))
                action = "dirty"
            self.stream_consumer.acknowledge(message_id)
            self.stream_consumer.clear_failure(message_id)
            return action
        except Exception as exc:
            logger.exception("cash valuation fact processing failed id=%s", message_id)
            failures = self.stream_consumer.increment_failure(message_id)
            if failures >= MAX_FAILURES:
                self.stream_consumer.publish_dead_letter(
                    source_message_id=message_id,
                    fields=fields,
                    error=repr(exc),
                )
                self.stream_consumer.acknowledge(message_id)
                self.stream_consumer.clear_failure(message_id)
                return "dead_letter"
            return "retry"

    def run_once(self) -> None:
        messages = self.stream_consumer.claim_stale_messages(pending_idle_ms=30_000, batch_size=100)
        messages += self.stream_consumer.read_new_messages(batch_size=100, block_ms=500)
        for message_id, fields in messages:
            self.handle_message(message_id, fields)

    def run_forever(self) -> None:
        self.stream_consumer.ensure_group()
        self.service.rebuild_active_index()
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("cash valuation fact worker failed")
                self.stop_event.wait(1)


class CashSecurityValuationTickWorker:
    """Consume the standard tick stream without creating a second quote feed."""

    def __init__(self, *, redis, service: CashSecurityValuationService, stream_name: str = SECURITIES_MARKET_TICK_STREAM) -> None:
        self.service = service
        self.consumer_name = _consumer_name("cash-val-tick")
        self.stream_consumer = MarketTickStreamConsumer(
            redis,
            stream_name=stream_name,
            group_name=CASH_VALUATION_TICK_CONSUMER_GROUP,
            consumer_name=self.consumer_name,
            dead_letter_stream="stream:market-ticks:cash-valuation:dead-letter",
            failure_ttl_seconds=FAILURE_TTL_SECONDS,
            failure_key_factory=cash_valuation_tick_failure_key,
        )
        self.stop_event = Event()

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def run_once(self) -> int:
        count = 0
        messages = self.stream_consumer.claim_stale_messages(
            pending_idle_ms=30_000, batch_size=100
        )
        messages += self.stream_consumer.read_new_messages(batch_size=100, block_ms=500)
        for message_id, fields in messages:
            if fields is None:
                self.stream_consumer.acknowledge(message_id)
                self.stream_consumer.clear_failure(message_id)
                continue
            try:
                payload = json.loads(fields.get("payload", "{}"))
                self.service.mark_tick_dirty(
                    exchange_id=payload["exchange_id"],
                    order_book_id=payload["order_book_id"],
                    source_event_id=payload.get("source_event_id", fields.get("event_id", message_id)),
                )
                self.stream_consumer.acknowledge(message_id)
                self.stream_consumer.clear_failure(message_id)
                count += 1
            except Exception as exc:
                logger.exception("cash valuation tick processing failed id=%s", message_id)
                failures = self.stream_consumer.increment_failure(message_id)
                if failures >= MAX_FAILURES:
                    self.stream_consumer.publish_dead_letter(
                        source_message_id=message_id,
                        fields=fields,
                        error=repr(exc),
                    )
                    self.stream_consumer.acknowledge(message_id)
                    self.stream_consumer.clear_failure(message_id)
        return count

    def run_forever(self) -> None:
        self.stream_consumer.ensure_group()
        self.service.rebuild_active_index()
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("cash valuation tick worker failed")
                self.stop_event.wait(1)


class CashSecurityValuationPersistenceWorker:
    """批量持久化估值结果；它是唯一允许写入估值金额的组件。"""

    def __init__(self, *, service: CashSecurityValuationService) -> None:
        self.service = service
        self.stop_event = Event()
        self.owner = _consumer_name("cash-val-writer")
        self.fencing_token: str | None = None
        self.lease_ttl_seconds = 15

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def run_once(self):
        if self.fencing_token is None:
            self.fencing_token = self.service.store.acquire_writer_lease(
                self.owner, self.lease_ttl_seconds
            )
            if self.fencing_token is not None and not self.service.activate_writer_fence(
                owner=self.owner, fencing_token=self.fencing_token
            ):
                self.service.store.release_writer_lease(
                    self.owner, self.fencing_token
                )
                self.fencing_token = None
        elif not self.service.store.renew_writer_lease(
            self.owner, self.fencing_token, self.lease_ttl_seconds
        ):
            self.fencing_token = None
        if self.fencing_token is None:
            return None
        return self.service.persist_batch(
            100,
            lease_owner=self.owner,
            fencing_token=self.fencing_token,
        )

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("cash valuation persistence failed")
            self.stop_event.wait(0.5)
        if self.fencing_token is not None:
            self.service.store.release_writer_lease(
                self.owner, self.fencing_token
            )


def build_service() -> CashSecurityValuationService:
    return CashSecurityValuationService(
        session_factory=SessionLocal,
        store=CashSecurityValuationStore(redis_client),
        market_tick_store=MarketTickStore(redis_client),
    )


def main() -> None:
    # 生产环境可分别启动事实、Tick 和持久化 Worker；直接运行本模块时默认启动
    # 持久化写入器，避免多个入口同时写入账户估值金额。
    worker = CashSecurityValuationPersistenceWorker(service=build_service())
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)
    try:
        worker.run_forever()
    finally:
        redis_client.close()


if __name__ == "__main__":
    main()
