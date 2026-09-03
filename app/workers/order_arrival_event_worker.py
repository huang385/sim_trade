"""撮合进程内的订单到达触发器；订单索引消费者不再执行撮合。"""

import json
import logging
from threading import Event

from app.enums.market_feed_enums import MarketFeedDomain, resolve_market_feed_domain


logger = logging.getLogger(__name__)


class OrderArrivalEventWorker:
    """消费订单事实，并在所属行情域中触发一次即时撮合。"""

    ACCEPTED_EVENTS = frozenset(
        {
            "ORDER_ACCEPTED",
            "STOCK_ORDER_ACCEPTED",
            "CONVERTIBLE_BOND_ORDER_ACCEPTED",
            "ETF_ORDER_ACCEPTED",
        }
    )
    ACTIVE_STATUSES = frozenset({"ACCEPTED", "PARTIALLY_FILLED"})

    def __init__(
        self,
        *,
        domain: MarketFeedDomain,
        session_factory,
        stream_consumer,
        order_repository,
        arrival_matching_service,
        market_order_execution_service=None,
        batch_size: int,
        block_ms: int,
        pending_idle_ms: int,
        max_retries: int,
        retry_interval_seconds: float,
    ):
        self.domain = domain
        self.session_factory = session_factory
        self.stream_consumer = stream_consumer
        self.order_repository = order_repository
        self.arrival_matching_service = arrival_matching_service
        self.market_order_execution_service = market_order_execution_service
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.pending_idle_ms = pending_idle_ms
        self.max_retries = max_retries
        self.retry_interval_seconds = retry_interval_seconds
        self.stop_event = Event()

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def _process(self, fields: dict[str, str]) -> str:
        event_type = str(fields.get("event_type") or "").strip()
        if event_type not in self.ACCEPTED_EVENTS:
            return "IGNORED"
        try:
            payload = json.loads(fields.get("payload", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("订单到达事件 payload 不是合法 JSON") from exc
        order_id = str(payload.get("order_id") or "").strip()
        if not order_id:
            raise ValueError("订单到达事件缺少 order_id")
        with self.session_factory() as db:
            order = self.order_repository.get_by_order_id(db, order_id)
        if order is None or resolve_market_feed_domain(order.instrument_type) != self.domain:
            return "IGNORED"
        if str(order.status) not in self.ACTIVE_STATUSES or order.remaining_volume <= 0:
            return "IGNORED"
        order_type = getattr(order.order_type, "value", order.order_type)
        if (
            self.market_order_execution_service is not None
            and str(order_type) == "MARKET"
        ):
            self.market_order_execution_service.execute(order_id=order_id)
            return "MARKET_EXECUTED"
        result = self.arrival_matching_service.match_if_ready(
            order_id=order.order_id,
            exchange_id=order.exchange_id,
            order_book_id=order.order_book_id,
            symbol=order.symbol,
        )
        return result.action

    def handle_message(self, message_id: str, fields: dict[str, str] | None) -> str:
        if fields is None:
            self.stream_consumer.acknowledge(message_id)
            self.stream_consumer.clear_failure(message_id)
            return "acknowledged"
        try:
            self._process(fields)
            self.stream_consumer.acknowledge(message_id)
            self.stream_consumer.clear_failure(message_id)
            return "acknowledged"
        except Exception as exc:
            failures = self.stream_consumer.increment_failure(message_id)
            if failures < self.max_retries:
                logger.warning(
                    "订单到达撮合失败，保留 Pending id=%s domain=%s retry=%s",
                    message_id,
                    self.domain.value,
                    failures,
                )
                return "retry"
            self.stream_consumer.publish_dead_letter(
                source_message_id=message_id,
                fields=fields,
                error=f"{type(exc).__name__}: {exc}",
            )
            self.stream_consumer.acknowledge(message_id)
            self.stream_consumer.clear_failure(message_id)
            return "dead_lettered"

    def run_once(self) -> None:
        messages = self.stream_consumer.claim_stale_messages(
            pending_idle_ms=self.pending_idle_ms,
            batch_size=self.batch_size,
        )
        messages += self.stream_consumer.read_new_messages(
            batch_size=self.batch_size,
            block_ms=self.block_ms,
        )
        for message_id, fields in messages:
            self.handle_message(message_id, fields)

    def run_forever(self) -> None:
        group_ready = False
        while not self.stop_event.is_set():
            try:
                if not group_ready:
                    self.stream_consumer.ensure_group()
                    group_ready = True
                self.run_once()
            except Exception:
                logger.exception("订单到达撮合循环异常 domain=%s", self.domain.value)
                self.stop_event.wait(self.retry_interval_seconds)
