"""Consumers that route cash-security ticks and facts to the valuation queue."""

import json
import logging
import os
import signal
import socket
from threading import Event

from redis.exceptions import ResponseError

from app.core.database import SessionLocal
from app.core.redis_client import redis_client
from app.infrastructure.cash_security_valuation_store import CashSecurityValuationStore
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.order_stream_consumer import OrderStreamConsumer
from app.infrastructure.redis_keys import (
    CASH_VALUATION_TICK_CONSUMER_GROUP,
    MARKET_TICK_STREAM,
)
from app.services.cash_security_valuation_service import (
    CASH_TYPES,
    CashSecurityValuationService,
)


logger = logging.getLogger(__name__)


def _consumer_name(prefix: str) -> str:
    return f"{prefix}-{socket.gethostname()}-{os.getpid()}"


class CashSecurityValuationFactWorker:
    """Invalidate/rebuild only cash valuation state after committed facts."""

    def __init__(self, *, stream_consumer: OrderStreamConsumer, service: CashSecurityValuationService) -> None:
        self.stream_consumer = stream_consumer
        self.service = service
        self.stop_event = Event()

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def handle_message(self, message_id: str, fields: dict[str, str]) -> str:
        try:
            payload = json.loads(fields.get("payload", "{}"))
            if payload.get("fact_reason") == "CASH_SECURITY_REALTIME_VALUATION":
                # This worker is the producer of these absolute facts.  Feeding
                # them back into Dirty would create a valuation/event loop.
                action = "self_valuation_ignored"
            elif payload.get("instrument_type") not in CASH_TYPES and payload.get("account_type") not in {"SECURITIES_CASH", "STOCK"}:
                action = "ignored"
            else:
                # Position changes alter the rebuildable routing index.  The
                # authoritative row is read after the outbox transaction.
                if payload.get("event_type") in {"TRADE_CREATED", "POSITION_UPDATED", "POSITION_CLOSED", "DAILY_SETTLEMENT_REPLAY"}:
                    self.service.rebuild_active_index()
                account_id = payload.get("account_id")
                if account_id:
                    self.service.mark_account_dirty(account_id=account_id, source_event_id=payload.get("event_id", message_id))
                action = "dirty"
            self.stream_consumer.acknowledge(message_id)
            self.stream_consumer.clear_failure(message_id)
            return action
        except Exception:
            logger.exception("cash valuation fact processing failed id=%s", message_id)
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

    def __init__(self, *, redis, service: CashSecurityValuationService, stream_name: str = MARKET_TICK_STREAM) -> None:
        self.redis = redis
        self.service = service
        self.stream_name = stream_name
        self.consumer_name = _consumer_name("cash-val-tick")
        self.stop_event = Event()

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def ensure_group(self) -> None:
        try:
            self.redis.xgroup_create(self.stream_name, CASH_VALUATION_TICK_CONSUMER_GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def run_once(self) -> int:
        rows = self.redis.xreadgroup(CASH_VALUATION_TICK_CONSUMER_GROUP, self.consumer_name, {self.stream_name: ">"}, count=100, block=500)
        count = 0
        for _stream, messages in rows:
            for message_id, fields in messages:
                try:
                    payload = json.loads(fields.get("payload", "{}"))
                    self.service.mark_tick_dirty(
                        exchange_id=payload["exchange_id"], order_book_id=payload["order_book_id"],
                        source_event_id=payload.get("source_event_id", fields.get("event_id", message_id)),
                    )
                    self.redis.xack(self.stream_name, CASH_VALUATION_TICK_CONSUMER_GROUP, message_id)
                    count += 1
                except Exception:
                    logger.exception("cash valuation tick processing failed id=%s", message_id)
        return count

    def run_forever(self) -> None:
        self.ensure_group()
        self.service.rebuild_active_index()
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("cash valuation tick worker failed")
                self.stop_event.wait(1)


class CashSecurityValuationPersistenceWorker:
    """Batch persistent writer; it is the only component that writes money."""

    def __init__(self, *, service: CashSecurityValuationService) -> None:
        self.service = service
        self.stop_event = Event()

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def run_once(self):
        return self.service.persist_batch(100)

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("cash valuation persistence failed")
            self.stop_event.wait(0.5)


def build_service() -> CashSecurityValuationService:
    return CashSecurityValuationService(
        session_factory=SessionLocal,
        store=CashSecurityValuationStore(redis_client),
        market_tick_store=MarketTickStore(redis_client),
    )


def main() -> None:
    # A supervisor may launch fact/tick/persistence instances separately.  The
    # persistence loop is the safe default command entry point.
    worker = CashSecurityValuationPersistenceWorker(service=build_service())
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)
    try:
        worker.run_forever()
    finally:
        redis_client.close()


if __name__ == "__main__":
    main()
