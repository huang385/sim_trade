import logging
import queue
import signal
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.logging_config import setup_logging
from app.core.redis_client import redis_client
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.market_data.market_tick_store import (
    MarketTickStore,
    MarketTickStoreResult,
)
from app.infrastructure.market_data.remote_feed_client import (
    RemoteFeedClient,
    create_remote_sdk_client,
)
from app.repositories.instrument_repository import InstrumentRepository
from app.services.market_data_service import MarketDataService
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


@dataclass
class MarketDataWorkerStats:
    """行情Worker累计统计；访问时由Worker内部锁保护。"""

    received_count: int = 0
    enqueued_count: int = 0
    processed_count: int = 0
    published_count: int = 0
    duplicate_count: int = 0
    stale_count: int = 0
    invalid_count: int = 0
    queue_full_drop_count: int = 0
    processing_error_count: int = 0
    no_tick_count: int = 0
    reconnect_count: int = 0


@dataclass(frozen=True)
class QueuedTick:
    data: dict[str, Any]
    raw: dict[str, Any]


class MarketDataSubscriberWorker:
    """
    发现活动合约、管理SDK订阅，并用独立线程处理有界队列中的Tick。

    WebSocket回调只检查消息类型和非阻塞入队；数据库、Redis、标准化与
    校验全部在本地消费线程完成，单条坏Tick不会终止Worker。
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
        self.monotonic = monotonic

        self.tick_queue: queue.Queue[QueuedTick] = queue.Queue(
            maxsize=queue_size
        )
        self.stop_event = threading.Event()
        self._consumer_thread: threading.Thread | None = None
        self._subscription = None
        self._stats = MarketDataWorkerStats()
        self._stats_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._reconnect_delay = reconnect_initial_seconds
        self._next_reconnect_at = 0.0
        self._ever_started = False
        self.last_disconnect_at: datetime | None = None
        self.last_reconnect_at: datetime | None = None
        self.last_error: str = ""

    def _increment(self, field_name: str, amount: int = 1) -> None:
        with self._stats_lock:
            setattr(
                self._stats,
                field_name,
                getattr(self._stats, field_name) + amount,
            )

    def stats_snapshot(self) -> MarketDataWorkerStats:
        with self._stats_lock:
            return MarketDataWorkerStats(**asdict(self._stats))

    def request_stop(self, *_args) -> None:
        """SIGINT/SIGTERM只设置停止标记，资源由主循环finally统一释放。"""

        self.stop_event.set()

    def on_quote(self, data: dict[str, Any], raw: dict[str, Any]) -> None:
        """SDK后台线程回调：只做类型检查和put_nowait，必须快速返回。"""

        self._increment("received_count")
        if raw.get("type") != "tick":
            return
        try:
            self.tick_queue.put_nowait(
                QueuedTick(data=dict(data), raw=dict(raw))
            )
            self._increment("enqueued_count")
        except queue.Full:
            self._increment("queue_full_drop_count")

    def on_subscribe(self, report: dict[str, Any]) -> None:
        """处理逐合约订阅回执，不记录任何用户名、Token或完整原始报文。"""

        contracts = report.get("contracts") or {}
        has_success = False
        for code, item in contracts.items():
            if not item.get("exists", False):
                logger.warning("行情合约不存在 code=%s reason=CONTRACT_NOT_FOUND", code)
                continue
            if not item.get("is_live", False):
                logger.warning("行情合约非存续状态 code=%s reason=CONTRACT_NOT_LIVE", code)
                continue
            if not item.get("subscribed", False):
                logger.warning("行情合约订阅失败 code=%s reason=SUBSCRIBE_FAILED", code)
                continue
            has_success = True
            if item.get("session_state") == "idle":
                logger.info("行情合约当前不在采集时段 code=%s session_state=idle", code)
        if has_success:
            self._reset_reconnect_backoff()

    def on_message(self, message: dict[str, Any]) -> None:
        """INGESTION_STOPPED属于正常采集状态，不作为订阅程序故障。"""

        if (
            message.get("type") == "status"
            and message.get("code") == "INGESTION_STOPPED"
        ):
            logger.info("行情采集当前停止 code=INGESTION_STOPPED")

    def on_error(self, error: dict[str, Any]) -> None:
        """只保存错误代码，避免完整错误结构意外携带连接凭证。"""

        raw = error.get("raw") or {}
        code = str(raw.get("code") or "REMOTE_MARKET_DATA_ERROR")
        with self._state_lock:
            self.last_error = code
        logger.error("行情订阅运行期错误 code=%s", code)

    def _reset_reconnect_backoff(self) -> None:
        with self._state_lock:
            self._reconnect_delay = self.reconnect_initial_seconds
            self.last_error = ""

    def _process_queued_tick(self, item: QueuedTick) -> None:
        try:
            result = self.market_data_service.process_with_session_factory(
                self.session_factory,
                data=item.data,
                raw=item.raw,
            )
            self._increment("processed_count")
            if result.action == MarketTickStoreResult.PUBLISHED:
                self._increment("published_count")
            elif result.action == MarketTickStoreResult.DUPLICATE:
                self._increment("duplicate_count")
            elif result.action == MarketTickStoreResult.STALE:
                self._increment("stale_count")
            self._reset_reconnect_backoff()
        except (MarketTickValidationError, MarketTickNormalizationError, ValueError):
            self._increment("invalid_count")
        except Exception:
            self._increment("processing_error_count")
            logger.exception("单条行情处理异常")

    def _consume_loop(self) -> None:
        while not self.stop_event.is_set() or not self.tick_queue.empty():
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

    def _stop_subscription(self) -> None:
        subscription = self._subscription
        self._subscription = None
        if subscription is None:
            return
        try:
            subscription.stop()
        finally:
            subscription.join(timeout=5)

    def _enqueue_rest_snapshots(self, codes: frozenset[str]) -> None:
        snapshots = self.feed_client.get_latest_ticks(codes)
        for code in sorted(codes):
            tick = snapshots.get(code)
            if tick is None:
                self._increment("no_tick_count")
                continue
            raw = {
                "type": "tick",
                "event": "update",
                "channel": f"tick.{code}",
                "data": tick,
                "server_time": tick.get("server_time"),
            }
            self.on_quote(tick, raw)

    def _start_subscription(self, codes: frozenset[str]) -> None:
        """先恢复REST最新快照，再建立非daemon WebSocket订阅。"""

        # 一次SQL批量预热订阅合约。后续Tick正常情况下不再创建数据库Session。
        with self.session_factory() as db:
            self.market_data_service.refresh_instrument_cache(db, codes)
        self._enqueue_rest_snapshots(codes)
        subscription = self.feed_client.start_tick_callbacks(
            codes,
            on_quote=self.on_quote,
            on_subscribe=self.on_subscribe,
            on_message=self.on_message,
            on_error=self.on_error,
        )
        self._subscription = subscription
        if self._ever_started:
            self._increment("reconnect_count")
        self._ever_started = True
        self.last_reconnect_at = utc_now()
        self.subscription_service.mark_applied(codes)

    def _mark_disconnected(self, now: float) -> None:
        self._stop_subscription()
        self.last_disconnect_at = utc_now()
        self._next_reconnect_at = now + self._reconnect_delay
        self._reconnect_delay = min(
            self._reconnect_delay * 2,
            self.reconnect_max_seconds,
        )

    def _try_start(self, codes: frozenset[str], now: float) -> None:
        if now < self._next_reconnect_at:
            return
        try:
            self._start_subscription(codes)
        except Exception as exc:
            self.last_error = type(exc).__name__
            logger.warning(
                "行情订阅建立失败，等待重连 error_type=%s",
                type(exc).__name__,
            )
            self._next_reconnect_at = now + self._reconnect_delay
            self._reconnect_delay = min(
                self._reconnect_delay * 2,
                self.reconnect_max_seconds,
            )

    def _publish_source_status(self) -> None:
        stats = self.stats_snapshot()
        values = asdict(stats)
        values.update(
            {
                "source": "YML_FEEDHUB",
                "status": "STOPPING" if self.stop_event.is_set() else "RUNNING",
                "subscribed_codes": ",".join(
                    sorted(self.subscription_service.current_codes)
                ),
                "last_disconnect_at": self.last_disconnect_at,
                "last_reconnect_at": self.last_reconnect_at,
                "last_error": self.last_error,
                "updated_at": utc_now(),
            }
        )
        try:
            self.tick_store.update_source_status(values)
        except Exception:
            logger.warning("行情源状态写入Redis失败", exc_info=True)

    def run_once(self) -> None:
        """执行一次活动合约发现、订阅变化和断线恢复状态机。"""

        now = self.monotonic()
        if self._subscription is not None and not self._subscription.is_alive():
            self._mark_disconnected(now)

        desired_codes = self.subscription_service.get_desired_codes()
        change = self.subscription_service.observe(desired_codes, now=now)
        if change is not None:
            self._stop_subscription()
            if not change.codes:
                self.subscription_service.mark_applied(frozenset())
            else:
                self._try_start(change.codes, now)
        elif (
            self._subscription is None
            and self.subscription_service.current_codes
        ):
            # 当前集合未变化但连接已退出，按指数退避恢复同一组订阅。
            self._try_start(self.subscription_service.current_codes, now)

        self._publish_source_status()

    def shutdown(self) -> None:
        """按订阅线程、本地消费线程的顺序平滑释放资源。"""

        self.stop_event.set()
        self._stop_subscription()
        if self._consumer_thread is not None:
            self._consumer_thread.join(timeout=10)
        self._publish_source_status()

    def run_forever(self) -> None:
        self.start_consumer_thread()
        try:
            while not self.stop_event.is_set():
                try:
                    self.run_once()
                except Exception:
                    # Redis活动索引临时不可用也不能使长期Worker永久退出。
                    self._increment("processing_error_count")
                    logger.exception("行情订阅主循环异常")
                self.stop_event.wait(self.refresh_seconds)
        finally:
            self.shutdown()


def build_worker() -> MarketDataSubscriberWorker:
    """使用生产配置组装行情Worker，函数本身不记录任何凭证。"""

    tick_store = MarketTickStore(
        redis_client,
        stream_name=settings.market_tick_stream_name,
        processed_ttl_seconds=settings.market_tick_processed_ttl_seconds,
    )
    market_data_service = MarketDataService(
        instrument_repository=InstrumentRepository(),
        normalizer=MarketTickNormalizer(),
        validation_service=MarketTickValidationService(),
        tick_store=tick_store,
    )
    subscription_service = MarketSubscriptionService(
        active_order_index=ActiveOrderIndex(redis_client),
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
        refresh_seconds=(
            settings.remote_market_data_subscription_refresh_seconds
        ),
        reconnect_initial_seconds=(
            settings.remote_market_data_reconnect_initial_seconds
        ),
        reconnect_max_seconds=(
            settings.remote_market_data_reconnect_max_seconds
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
        redis_client.close()


if __name__ == "__main__":
    main()
