import logging
import queue
import signal
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.logging_config import setup_logging
from app.core.redis_client import redis_client
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.market_data.market_tick_store import (
    MarketTickStore,
)
from app.infrastructure.market_data.remote_feed_client import (
    RemoteFeedClient,
    create_remote_sdk_client,
)
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.repositories.instrument_repository import InstrumentRepository
from app.schemas.market_tick_schema import MarketTickIngestType
from app.services.market_data_service import (
    MarketDataProcessAction,
    MarketDataService,
)
from app.services.market_subscription_service import MarketSubscriptionService
from app.services.market_tick_normalizer import (
    MarketTickNormalizationError,
    MarketTickNormalizer,
)
from app.services.market_tick_validation_service import (
    MarketTickValidationError,
    MarketTickValidationService,
)


logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MarketDataSourceStatus(str, Enum):
    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


@dataclass
class MarketDataWorkerStats:
    """行情 Worker 累计统计；访问时由 Worker 内部锁保护。"""

    received_count: int = 0
    enqueued_count: int = 0
    processed_count: int = 0
    published_count: int = 0
    invalid_count: int = 0
    queue_full_drop_count: int = 0
    shutdown_drop_count: int = 0
    processing_error_count: int = 0
    reconnect_count: int = 0


@dataclass(frozen=True)
class QueuedTick:
    data: dict[str, Any]
    raw: dict[str, Any]
    ingest_type: MarketTickIngestType = MarketTickIngestType.LIVE_CALLBACK
    # Tick入队时所属的订阅代次；None只用于兼容独立测试和手工调用。
    subscription_generation: int | None = None


class MarketDataSubscriberWorker:
    """
    发现活动合约、管理 SDK 订阅，并用独立线程处理有界队列中的 Tick。

    WebSocket 回调只做轻量检查和非阻塞入队；数据库、Redis、标准化与
    校验全部由本地消费线程执行，单条坏 Tick 不会终止 Worker。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        feed_client: RemoteFeedClient,
        market_data_service: MarketDataService,
        subscription_service: MarketSubscriptionService,
        tick_store: MarketTickStore,
        queue_size: int,
        refresh_seconds: float,
        reconnect_initial_seconds: float,
        reconnect_max_seconds: float,
        shutdown_drain_timeout_seconds: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.session_factory = session_factory
        self.feed_client = feed_client
        self.market_data_service = market_data_service
        self.subscription_service = subscription_service
        self.tick_store = tick_store
        self.refresh_seconds = refresh_seconds
        self.reconnect_initial_seconds = reconnect_initial_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.shutdown_drain_timeout_seconds = shutdown_drain_timeout_seconds
        self.monotonic = monotonic

        self.tick_queue: queue.Queue[QueuedTick] = queue.Queue(maxsize=queue_size)
        self.stop_event = threading.Event()
        self._force_consumer_stop = threading.Event()
        self._consumer_thread: threading.Thread | None = None
        self._subscription = None
        self._accepting_ticks = True
        self._stats = MarketDataWorkerStats()
        self._stats_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False
        self._status = MarketDataSourceStatus.IDLE
        self._desired_codes: frozenset[str] = frozenset()
        self._reconnect_delay = reconnect_initial_seconds
        self._next_reconnect_at = 0.0
        self._retry_generation: int | None = None
        self._disconnected_waiting = False
        self._ever_started = False
        self._last_queue_drop_at: float | None = None
        self.last_tick_at: datetime | None = None
        self.last_published_at: datetime | None = None
        self.last_disconnect_at: datetime | None = None
        self.last_reconnect_at: datetime | None = None
        self.last_successful_subscribe_at: datetime | None = None
        self.last_error: str = ""

    def _increment(self, field_name: str, amount: int = 1) -> None:
        with self._stats_lock:
            setattr(self._stats, field_name, getattr(self._stats, field_name) + amount)

    def stats_snapshot(self) -> MarketDataWorkerStats:
        with self._stats_lock:
            return MarketDataWorkerStats(**asdict(self._stats))

    def _set_status(self, status: MarketDataSourceStatus) -> None:
        with self._state_lock:
            self._status = status

    def request_stop(self, *_args) -> None:
        """SIGINT/SIGTERM 只发出停止请求，资源由主循环 finally 统一释放。"""

        self.stop_event.set()
        self._set_status(MarketDataSourceStatus.STOPPING)

    def on_quote(
        self,
        data: dict[str, Any],
        raw: dict[str, Any],
        *,
        generation: int | None = None,
    ) -> None:
        """SDK 回调：记录接收时间并非阻塞入队，不执行数据库或 Redis 操作。"""

        with self._state_lock:
            if not self._accepting_ticks:
                return
        self._increment("received_count")
        if raw.get("type") != "tick":
            return
        with self._state_lock:
            self.last_tick_at = utc_now()
        try:
            self.tick_queue.put_nowait(
                QueuedTick(
                    data=dict(data),
                    raw=dict(raw),
                    ingest_type=MarketTickIngestType.LIVE_CALLBACK,
                    subscription_generation=generation,
                )
            )
            self._increment("enqueued_count")
        except queue.Full:
            self._increment("queue_full_drop_count")
            with self._state_lock:
                self._last_queue_drop_at = self.monotonic()

    def _schedule_failed_subscription_retry(self, generation: int) -> None:
        """同一代失败回执只安排一次重试，重复回执不会加速退避。"""

        with self._state_lock:
            if generation != self.subscription_service.state_snapshot().generation:
                return
            if self._retry_generation == generation:
                return
            self._retry_generation = generation
            self._next_reconnect_at = self.monotonic() + self._reconnect_delay
            self._reconnect_delay = min(
                self._reconnect_delay * 2,
                self.reconnect_max_seconds,
            )
            self._status = MarketDataSourceStatus.DEGRADED
            self.last_error = "SUBSCRIPTION_PARTIAL_FAILURE"

    def on_subscribe(
        self,
        report: dict[str, Any],
        *,
        generation: int | None = None,
    ) -> None:
        """幂等处理异步逐合约订阅回执，不记录原始报文或任何凭证。"""

        state = self.subscription_service.apply_subscription_report(
            report,
            generation=generation,
        )
        if generation is not None and generation != state.generation:
            return

        contracts = report.get("contracts") or {}
        for code in sorted(state.failed_codes):
            if code in contracts:
                logger.warning(
                    "行情合约订阅失败 code=%s reason=%s",
                    code,
                    state.failure_reasons[code],
                )

        if state.all_subscribed:
            with self._state_lock:
                self._reconnect_delay = self.reconnect_initial_seconds
                self._next_reconnect_at = 0.0
                self._retry_generation = None
                self.last_error = ""
                self.last_successful_subscribe_at = utc_now()
                self._status = MarketDataSourceStatus.RUNNING
            for code, item in contracts.items():
                if item.get("session_state") == "idle":
                    logger.info(
                        "行情合约当前不在采集时段 code=%s session_state=idle",
                        code,
                    )
        elif state.failed_codes:
            self._schedule_failed_subscription_retry(state.generation)

    def on_message(self, message: dict[str, Any]) -> None:
        """INGESTION_STOPPED 属于正常采集状态，不作为订阅故障。"""

        if (
            message.get("type") == "status"
            and message.get("code") == "INGESTION_STOPPED"
        ):
            logger.info("行情采集当前停止 code=INGESTION_STOPPED")

    def on_error(self, error: dict[str, Any]) -> None:
        """只保存安全错误代码，避免完整错误结构意外携带连接凭证。"""

        raw = error.get("raw") or {}
        code = str(raw.get("code") or "REMOTE_MARKET_DATA_ERROR")
        with self._state_lock:
            self.last_error = code
        logger.error("行情订阅运行期错误 code=%s", code)

    def _process_queued_tick(self, item: QueuedTick) -> None:
        try:
            result = self.market_data_service.process_with_session_factory(
                self.session_factory,
                data=item.data,
                raw=item.raw,
                ingest_type=item.ingest_type,
                subscription_generation=item.subscription_generation,
            )
            self._increment("processed_count")
            if result.action == MarketDataProcessAction.PUBLISHED:
                self._increment("published_count")
                with self._state_lock:
                    self.last_published_at = utc_now()
        except (MarketTickValidationError, MarketTickNormalizationError, ValueError):
            self._increment("invalid_count")
        except Exception:
            self._increment("processing_error_count")
            logger.exception("单条行情处理异常")

    def _consume_loop(self) -> None:
        while True:
            if self._force_consumer_stop.is_set():
                return
            if self.stop_event.is_set() and self.tick_queue.empty():
                return
            try:
                item = self.tick_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process_queued_tick(item)
            finally:
                self.tick_queue.task_done()

    def start_consumer_thread(self) -> None:
        if self._consumer_thread is not None and self._consumer_thread.is_alive():
            return
        self._consumer_thread = threading.Thread(
            target=self._consume_loop,
            name="MarketTickProcessor",
            daemon=False,
        )
        self._consumer_thread.start()

    def _stop_subscription(self, *, wait_until_stopped: bool = False) -> None:
        subscription = self._subscription
        self._subscription = None
        if subscription is None:
            return
        try:
            subscription.stop()
        finally:
            subscription.join(timeout=5)
            if wait_until_stopped and subscription.is_alive():
                # 最终退出前必须确认 SDK 的非 daemon 线程已经结束。
                subscription.join()

    def _start_subscription(self, codes: frozenset[str]) -> None:
        """预热本地合约缓存后直接建立 WebSocket 实时行情订阅。"""

        generation = self.subscription_service.mark_requested(codes)
        with self._state_lock:
            self._status = MarketDataSourceStatus.CONNECTING
            self._disconnected_waiting = False
            self._retry_generation = None

        # 一次 SQL 批量预热；后续正常 Tick 不再创建数据库 Session。
        with self.session_factory() as db:
            self.market_data_service.refresh_instrument_cache(db, codes)
        subscription = self.feed_client.start_tick_callbacks(
            codes,
            on_quote=lambda data, raw: self.on_quote(
                data,
                raw,
                generation=generation,
            ),
            on_subscribe=lambda report: self.on_subscribe(
                report,
                generation=generation,
            ),
            on_message=self.on_message,
            on_error=self.on_error,
        )
        self._subscription = subscription
        if self._ever_started:
            self._increment("reconnect_count")
        self._ever_started = True
        with self._state_lock:
            self.last_reconnect_at = utc_now()

    def _mark_disconnected(self, now: float) -> None:
        self._stop_subscription()
        with self._state_lock:
            self.last_disconnect_at = utc_now()
            self._status = MarketDataSourceStatus.DISCONNECTED
            self._disconnected_waiting = True
            self._next_reconnect_at = now + self._reconnect_delay
            self._reconnect_delay = min(
                self._reconnect_delay * 2,
                self.reconnect_max_seconds,
            )

    def _try_start(self, codes: frozenset[str], now: float) -> None:
        with self._state_lock:
            if now < self._next_reconnect_at:
                self._status = MarketDataSourceStatus.DISCONNECTED
                self._disconnected_waiting = True
                return
            self._status = MarketDataSourceStatus.CONNECTING
            self._disconnected_waiting = False
        try:
            self._start_subscription(codes)
        except Exception as exc:
            with self._state_lock:
                self.last_error = type(exc).__name__
                self.last_disconnect_at = utc_now()
                self._status = MarketDataSourceStatus.DISCONNECTED
                self._disconnected_waiting = True
                self._next_reconnect_at = now + self._reconnect_delay
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self.reconnect_max_seconds,
                )
            logger.warning(
                "行情订阅建立失败，等待重连 error_type=%s",
                type(exc).__name__,
            )

    def _recent_queue_drops(self, now: float) -> bool:
        with self._state_lock:
            if self._last_queue_drop_at is None:
                return False
            window = max(5.0, self.refresh_seconds * 3)
            return now - self._last_queue_drop_at <= window

    def _derive_status(self, now: float) -> MarketDataSourceStatus:
        with self._state_lock:
            if self._status == MarketDataSourceStatus.STOPPED:
                return self._status
            if self.stop_event.is_set():
                return MarketDataSourceStatus.STOPPING
            disconnected_waiting = self._disconnected_waiting
        state = self.subscription_service.state_snapshot()
        if not self._desired_codes and not state.requested_codes:
            return MarketDataSourceStatus.IDLE
        if disconnected_waiting:
            return MarketDataSourceStatus.DISCONNECTED
        if state.failed_codes or self._recent_queue_drops(now):
            return MarketDataSourceStatus.DEGRADED
        if (
            self._subscription is not None
            and self._subscription.is_alive()
            and state.all_subscribed
        ):
            return MarketDataSourceStatus.RUNNING
        return MarketDataSourceStatus.CONNECTING

    def _publish_source_status(self, *, force_status: MarketDataSourceStatus | None = None) -> None:
        stats = self.stats_snapshot()
        state = self.subscription_service.state_snapshot()
        now = self.monotonic()
        status = force_status or self._derive_status(now)
        with self._state_lock:
            self._status = status
            values = asdict(stats)
            values.update(
                {
                    "source": "YML_FEEDHUB",
                    "status": status.value,
                    "subscription_generation": state.generation,
                    "requested_codes": ",".join(sorted(state.requested_codes)),
                    "subscribed_codes": ",".join(sorted(state.subscribed_codes)),
                    "failed_codes": ",".join(sorted(state.failed_codes)),
                    "requested_count": len(state.requested_codes),
                    "subscribed_count": len(state.subscribed_codes),
                    "failed_count": len(state.failed_codes),
                    "last_tick_at": self.last_tick_at,
                    "last_published_at": self.last_published_at,
                    "queue_current_size": self.tick_queue.qsize(),
                    "queue_capacity": self.tick_queue.maxsize,
                    "last_disconnect_at": self.last_disconnect_at,
                    "last_reconnect_at": self.last_reconnect_at,
                    "last_successful_subscribe_at": self.last_successful_subscribe_at,
                    "last_error": self.last_error,
                    "updated_at": utc_now(),
                }
            )
        try:
            self.tick_store.update_source_status(values)
        except Exception:
            logger.warning("行情源状态写入 Redis 失败", exc_info=True)

    def run_once(self) -> None:
        """执行一次目标发现、变更防抖、失败重试和断线恢复。"""

        now = self.monotonic()
        if self._subscription is not None and not self._subscription.is_alive():
            self._mark_disconnected(now)

        desired_codes = self.subscription_service.get_desired_codes()
        self._desired_codes = desired_codes
        change = self.subscription_service.observe(desired_codes, now=now)
        if change is not None:
            self._stop_subscription()
            if not change.codes:
                self.subscription_service.clear()
                with self._state_lock:
                    self._reconnect_delay = self.reconnect_initial_seconds
                    self._next_reconnect_at = 0.0
                    self._retry_generation = None
                    self._disconnected_waiting = False
                    self.last_error = ""
                    self._status = MarketDataSourceStatus.IDLE
            else:
                # 目标集合变化开始一轮新的退避周期。
                with self._state_lock:
                    self._reconnect_delay = self.reconnect_initial_seconds
                    self._next_reconnect_at = now
                    self._retry_generation = None
                self._try_start(change.codes, now)
        else:
            state = self.subscription_service.state_snapshot()
            if state.failed_codes and now >= self._next_reconnect_at:
                # SDK 不支持可靠追加订阅，因此退避到期后重建完整目标集合。
                self._stop_subscription()
                self._try_start(state.requested_codes, now)
            elif self._subscription is None and state.requested_codes:
                self._try_start(state.requested_codes, now)

        self._publish_source_status()

    def _drop_queued_items(self) -> int:
        dropped = 0
        while True:
            try:
                self.tick_queue.get_nowait()
            except queue.Empty:
                break
            else:
                self.tick_queue.task_done()
                dropped += 1
        if dropped:
            self._increment("shutdown_drop_count", dropped)
        return dropped

    def shutdown(self) -> None:
        """停止订阅、排空已有队列、终止消费线程，最后写入 STOPPED。"""

        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self.stop_event.set()
            self._set_status(MarketDataSourceStatus.STOPPING)
            self._publish_source_status(force_status=MarketDataSourceStatus.STOPPING)

            # 先停止 SDK，随后禁止回调继续向本地队列添加 Tick。
            self._stop_subscription(wait_until_stopped=True)
            with self._state_lock:
                self._accepting_ticks = False

            consumer = self._consumer_thread
            if consumer is not None and consumer.is_alive():
                consumer.join(timeout=self.shutdown_drain_timeout_seconds)
                if consumer.is_alive():
                    # 排空超时后明确放弃尚未领取的消息，并要求消费线程退出。
                    self._force_consumer_stop.set()
                    self._drop_queued_items()
                    consumer.join()
            else:
                self._drop_queued_items()

            self._set_status(MarketDataSourceStatus.STOPPED)
            self._publish_source_status(force_status=MarketDataSourceStatus.STOPPED)
            self._shutdown_complete = True

    def run_forever(self) -> None:
        self.start_consumer_thread()
        try:
            while not self.stop_event.is_set():
                try:
                    self.run_once()
                except Exception:
                    # Redis 临时不可用只影响本轮，不能使长期 Worker 永久退出。
                    self._increment("processing_error_count")
                    logger.exception("行情订阅主循环异常")
                self.stop_event.wait(self.refresh_seconds)
        finally:
            self.shutdown()


def build_worker() -> MarketDataSubscriberWorker:
    """使用生产配置组装 Worker，本函数不记录用户名、Token 或完整地址。"""

    tick_store = MarketTickStore(
        redis_client,
        stream_name=settings.market_tick_stream_name,
    )
    market_data_service = MarketDataService(
        instrument_repository=InstrumentRepository(),
        normalizer=MarketTickNormalizer(),
        validation_service=MarketTickValidationService(),
        tick_store=tick_store,
    )
    subscription_service = MarketSubscriptionService(
        active_order_index=ActiveOrderIndex(redis_client),
        # 活动持仓合约索引由实时盈亏Worker维护在Redis中。复用该索引可让
        # 已成交持仓继续接收行情，同时避免订阅Worker高频查询PostgreSQL。
        active_position_contract_source=RealtimePnlStore(redis_client),
        debounce_seconds=(
            settings.remote_market_data_subscription_debounce_seconds
        ),
    )
    return MarketDataSubscriberWorker(
        session_factory=SessionLocal,
        feed_client=RemoteFeedClient(create_remote_sdk_client(settings)),
        market_data_service=market_data_service,
        subscription_service=subscription_service,
        tick_store=tick_store,
        queue_size=settings.remote_market_data_queue_size,
        refresh_seconds=settings.remote_market_data_subscription_refresh_seconds,
        reconnect_initial_seconds=(
            settings.remote_market_data_reconnect_initial_seconds
        ),
        reconnect_max_seconds=settings.remote_market_data_reconnect_max_seconds,
        shutdown_drain_timeout_seconds=(
            settings.remote_market_data_shutdown_drain_timeout_seconds
        ),
    )


def main() -> None:
    """命令行入口：python -m app.workers.market_data_subscriber_worker。"""

    setup_logging()
    worker = build_worker()
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)
    try:
        worker.run_forever()
    finally:
        # shutdown 已保证消费线程退出，Redis 不会被仍在运行的线程继续使用。
        redis_client.close()


if __name__ == "__main__":
    main()
