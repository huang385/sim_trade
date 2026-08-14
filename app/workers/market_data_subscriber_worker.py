import logging
import queue
import signal
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
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
from app.infrastructure.market_data.database_snapshot_client import (
    YMM_DATABASE_SOURCE,
    YmmDatabaseSnapshotClient,
)
from app.infrastructure.market_data.remote_feed_client import (
    RemoteFeedClient,
    create_remote_sdk_client,
    load_remote_sdk_client_class,
    remote_sdk_client_kwargs,
)
from app.modules.market_data.contracts import MarketDataProvider
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.infrastructure.database.repository_adapters import (
    InstrumentMarketDataMappingRepository,
    InstrumentRepository,
)
from app.schemas.market_tick_schema import MarketTickIngestType
from app.modules.market_data import (
    MarketDataProcessAction,
    MarketDataService,
)
from app.modules.market_data import (
    MarketDataCodeMappingService,
    MarketDataCodeMappingSnapshot,
)
from app.modules.market_data import MarketSubscriptionService
from app.infrastructure.market_pre_subscription_store import (
    MarketPreSubscriptionStore,
)
from app.infrastructure.client_market_subscription_store import (
    ClientMarketSubscriptionStore,
)
from app.modules.market_data import (
    MarketTickNormalizationError,
    MarketTickNormalizer,
)
from app.modules.market_data import (
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
    source: str = "YMM_LIVE_DATA"
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
        feed_client: MarketDataProvider,
        market_data_service: MarketDataService,
        code_mapping_service: MarketDataCodeMappingService,
        subscription_service: MarketSubscriptionService,
        tick_store: MarketTickStore,
        queue_size: int,
        refresh_seconds: float,
        reconnect_initial_seconds: float,
        reconnect_max_seconds: float,
        shutdown_drain_timeout_seconds: float = 10.0,
        database_snapshot_client=None,
        database_snapshot_retry_seconds: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.session_factory = session_factory
        self.feed_client = feed_client
        self.market_data_service = market_data_service
        self.code_mapping_service = code_mapping_service
        self.subscription_service = subscription_service
        self.tick_store = tick_store
        self.refresh_seconds = refresh_seconds
        self.reconnect_initial_seconds = reconnect_initial_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.shutdown_drain_timeout_seconds = shutdown_drain_timeout_seconds
        self.database_snapshot_client = database_snapshot_client
        self.database_snapshot_retry_seconds = max(
            database_snapshot_retry_seconds,
            1.0,
        )
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
        self._code_mapping = MarketDataCodeMappingSnapshot.identity([])
        self._callback_generation = 0
        # 订阅集合可在同一SDK连接内动态变更，因此业务代次会变化；连接代次只在
        # SDK客户端重建时变化，用于拒绝已经关闭的旧连接晚到回调。
        self._callback_connection_generation = 0
        self._source_issue: str = ""
        self._terminal_source_error = False
        self._reconnect_delay = reconnect_initial_seconds
        self._next_reconnect_at = 0.0
        self._retry_generation: int | None = None
        self._disconnected_waiting = False
        self._ever_started = False
        self._last_queue_drop_at: float | None = None
        # 坏Tick可能高频连续到达。这里只记录限频后的错误类型和标准合约代码，
        # 不打印完整行情报文，既保留排障信息也避免日志刷屏或泄露上游细节。
        self._last_invalid_tick_log_at: float | None = None
        self._last_storage_slow_consumer_log_at: float | None = None
        self._snapshot_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="MarketBootstrap",
        )
        self._snapshot_futures: set[Future] = set()
        self._bootstrap_requested_codes: set[str] = set()
        self._bootstrap_retry_at: dict[str, float] = {}
        self._latest_live_event_times: dict[str, datetime] = {}
        self._bootstrap_watermarks: dict[str, datetime] = {}
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

    def _record_invalid_tick(self, exc: Exception, code: object) -> None:
        """累计坏Tick，并且最多每5秒输出一条脱敏原因日志。"""

        self._increment("invalid_count")
        now = self.monotonic()
        with self._state_lock:
            if (
                self._last_invalid_tick_log_at is not None
                and now - self._last_invalid_tick_log_at < 5
            ):
                return
            self._last_invalid_tick_log_at = now
        logger.warning(
            "行情Tick校验失败 code=%s reason=%s detail=%s",
            str(code or "UNKNOWN").strip().upper(),
            type(exc).__name__,
            str(exc)[:300],
        )

    def _log_storage_slow_consumer(self) -> None:
        """行情中心存储告警最多每60秒输出一次，不影响实时订阅状态。"""

        now = self.monotonic()
        with self._state_lock:
            if (
                self._last_storage_slow_consumer_log_at is not None
                and now - self._last_storage_slow_consumer_log_at < 60
            ):
                return
            self._last_storage_slow_consumer_log_at = now
        logger.warning(
            "行情中心存储消费较慢，实时订阅继续运行 "
            "code=STORAGE_SLOW_CONSUMER"
        )

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
        code_mapping: MarketDataCodeMappingSnapshot | None = None,
    ) -> None:
        """SDK 回调：记录接收时间并非阻塞入队，不执行数据库或 Redis 操作。"""

        with self._state_lock:
            if not self._accepting_ticks:
                return
        self._increment("received_count")
        if (
            data.get("action") != "feed"
            or not str(data.get("channel") or "").startswith("tick_")
        ):
            return
        with self._state_lock:
            self.last_tick_at = utc_now()
        try:
            normalized_data = dict(data)
            normalized_data["local_recv_time"] = utc_now()
            with self._state_lock:
                active_mapping = code_mapping or self._code_mapping
                active_generation = (
                    generation
                    if generation is not None
                    else self._callback_generation
                )
            source_code = str(data.get("order_book_id") or "")
            if not active_mapping.source_codes and source_code.strip():
                active_mapping = MarketDataCodeMappingSnapshot.identity(
                    [source_code]
                )
            # 退订与回调可能短暂交错，当前代次已经不包含的旧频道不能
            # 污染新的subscription_generation。
            if source_code.strip().upper() not in active_mapping.source_codes:
                return
            normalized_data["order_book_id"] = active_mapping.to_internal(
                source_code
            )
            self.tick_queue.put_nowait(
                QueuedTick(
                    data=normalized_data,
                    raw=dict(raw),
                    ingest_type=MarketTickIngestType.LIVE_CALLBACK,
                    source="YMM_LIVE_DATA",
                    subscription_generation=active_generation,
                )
            )
            self._increment("enqueued_count")
        except ValueError as exc:
            self._record_invalid_tick(
                exc,
                data.get("order_book_id"),
            )
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
        code_mapping: MarketDataCodeMappingSnapshot | None = None,
    ) -> None:
        """幂等处理异步逐合约订阅回执，不记录原始报文或任何凭证。"""

        normalized_report = dict(report)
        contracts = report.get("contracts") or {}
        if code_mapping is not None:
            normalized_report["contracts"] = {
                code_mapping.to_internal(raw_code): item
                for raw_code, item in contracts.items()
            }

        state = self.subscription_service.apply_subscription_report(
            normalized_report,
            generation=generation,
        )
        if generation is not None and generation != state.generation:
            return

        contracts = normalized_report.get("contracts") or {}
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

        if code_mapping is not None:
            self._schedule_bootstrap_snapshots(
                state.subscribed_codes,
                generation=state.generation,
                code_mapping=code_mapping,
            )

    def _schedule_bootstrap_snapshots(
        self,
        codes: frozenset[str],
        *,
        generation: int,
        code_mapping: MarketDataCodeMappingSnapshot,
    ) -> None:
        """每个新增实际订阅只异步补取一次数据库最后Tick。"""

        if self.database_snapshot_client is None:
            return
        with self._state_lock:
            now = self.monotonic()
            added = {
                code
                for code in set(codes) - self._bootstrap_requested_codes
                if self._bootstrap_retry_at.get(code, 0.0) <= now
            }
            if not added or self.stop_event.is_set():
                return
            self._bootstrap_requested_codes.update(added)
        source_codes = {
            code_mapping.to_source(code)
            for code in added
        }
        future = self._snapshot_executor.submit(
            self.database_snapshot_client.fetch_latest_many,
            source_codes,
        )
        with self._state_lock:
            self._snapshot_futures.add(future)

        def completed(done: Future) -> None:
            with self._state_lock:
                self._snapshot_futures.discard(done)
            try:
                snapshots = done.result()
            except Exception as exc:
                retry_at = self.monotonic() + self.database_snapshot_retry_seconds
                with self._state_lock:
                    self._bootstrap_requested_codes.difference_update(added)
                    for code in added:
                        self._bootstrap_retry_at[code] = retry_at
                logger.warning(
                    "数据库初始化行情查询失败，等待低频重试 "
                    "error_type=%s retry_seconds=%s",
                    type(exc).__name__,
                    self.database_snapshot_retry_seconds,
                )
                return
            with self._state_lock:
                for code in added:
                    self._bootstrap_retry_at.pop(code, None)
            for source_code, data in snapshots.items():
                if self.stop_event.is_set():
                    return
                try:
                    internal_code = code_mapping.to_internal(source_code)
                    normalized_data = dict(data)
                    normalized_data["order_book_id"] = internal_code
                    normalized_data["channel"] = f"tick_{internal_code}"
                    self.tick_queue.put_nowait(
                        QueuedTick(
                            data=normalized_data,
                            raw={
                                "action": "feed",
                                "channel": f"tick_{internal_code}",
                            },
                            ingest_type=MarketTickIngestType.REST_SNAPSHOT,
                            source=YMM_DATABASE_SOURCE,
                            subscription_generation=generation,
                        )
                    )
                    self._increment("enqueued_count")
                except (ValueError, queue.Full) as exc:
                    if isinstance(exc, queue.Full):
                        self._increment("queue_full_drop_count")
                    else:
                        self._record_invalid_tick(exc, source_code)

        future.add_done_callback(completed)

    def on_message(self, message: dict[str, Any]) -> None:
        """把SDK状态变化收敛成Worker状态，不启动第二套网络重连循环。"""

        if message.get("type") != "status":
            return
        component = str(message.get("component") or "unknown")
        state = str(message.get("state") or "unknown")
        issue = f"{component}_{state}".upper()
        details = message.get("details") or {}
        if component == "storage" and state == "slow_consumer":
            # 行情中心历史存储管线积压不代表当前策略的实时订阅丢包。
            # 会话自身的 session/slow_consumer 仍按不可补发缺口处理。
            self._log_storage_slow_consumer()
            return
        terminal = state == "replaced" or (
            component == "session"
            and state == "disconnected"
            and details.get("reason") == "token_revoked"
        )
        broken = state in {
            "disconnected",
            "reconnecting",
            "partial",
            "quota_exceeded",
            "degraded",
            "slow_consumer",
            "error",
            "replaced",
        }
        with self._state_lock:
            if terminal:
                self._terminal_source_error = True
                self._source_issue = issue
                self.last_error = issue
            elif broken:
                self._source_issue = issue
                self._status = (
                    MarketDataSourceStatus.DISCONNECTED
                    if state in {"disconnected", "reconnecting"}
                    else MarketDataSourceStatus.DEGRADED
                )
            elif state == "recovered":
                # slow_consumer表示存在不可补发缺口，不能因为recovered
                # 自动恢复为完整行情，会话保持DEGRADED直到人工重启。
                if not self._source_issue.endswith("_SLOW_CONSUMER"):
                    self._source_issue = ""
                    self.last_error = ""
        if terminal:
            logger.error("行情SDK会话不可恢复 code=%s", issue)
            self.request_stop()

    def on_error(self, error: dict[str, Any]) -> None:
        """只保存安全错误代码，避免完整错误结构意外携带连接凭证。"""

        raw = error.get("raw") or {}
        code = str(raw.get("code") or "REMOTE_MARKET_DATA_ERROR")
        if code == "STORAGE_SLOW_CONSUMER":
            # 该告警来自行情中心的历史存储消费者，不是当前 WebSocket
            # 客户端消费变慢。实时 Tick 仍可连续到达时不应把行情订阅标成
            # 运行错误；保留 WARNING 便于服务端排查存储积压即可。
            self._log_storage_slow_consumer()
            return
        with self._state_lock:
            self.last_error = code
        logger.error("行情订阅运行期错误 code=%s", code)

    def _process_queued_tick(self, item: QueuedTick) -> None:
        try:
            code = str(item.data.get("order_book_id") or "").strip().upper()
            event_time = MarketTickNormalizer._datetime(
                item.data.get("datetime"),
                "datetime",
            )
            with self._state_lock:
                current_generation = (
                    self.subscription_service.state_snapshot().generation
                )
                if (
                    item.ingest_type == MarketTickIngestType.REST_SNAPSHOT
                    and item.subscription_generation is not None
                    and item.subscription_generation != current_generation
                ):
                    return
                if item.ingest_type == MarketTickIngestType.REST_SNAPSHOT:
                    latest_live = self._latest_live_event_times.get(code)
                    if latest_live is not None and latest_live >= event_time:
                        return
                else:
                    bootstrap_time = self._bootstrap_watermarks.get(code)
                    if bootstrap_time is not None and event_time <= bootstrap_time:
                        return
            result = self.market_data_service.process_with_session_factory(
                self.session_factory,
                data=item.data,
                raw=item.raw,
                ingest_type=item.ingest_type,
                source=item.source,
                subscription_generation=item.subscription_generation,
            )
            self._increment("processed_count")
            if result.action == MarketDataProcessAction.PUBLISHED:
                self._increment("published_count")
                with self._state_lock:
                    self.last_published_at = utc_now()
                    if item.ingest_type == MarketTickIngestType.REST_SNAPSHOT:
                        self._bootstrap_watermarks[code] = result.tick.event_time
                        logger.info(
                            "数据库初始化行情已接入 code=%s event_time=%s",
                            code,
                            result.tick.event_time.isoformat(),
                        )
                    else:
                        previous = self._latest_live_event_times.get(code)
                        if previous is None or result.tick.event_time > previous:
                            self._latest_live_event_times[code] = result.tick.event_time
                        bootstrap_time = self._bootstrap_watermarks.get(code)
                        if (
                            bootstrap_time is not None
                            and result.tick.event_time > bootstrap_time
                        ):
                            self._bootstrap_watermarks.pop(code, None)
        except (
            MarketTickValidationError,
            MarketTickNormalizationError,
            ValueError,
        ) as exc:
            self._record_invalid_tick(
                exc,
                item.data.get("order_book_id"),
            )
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
        with self._state_lock:
            subscription = self._subscription
            self._subscription = None
            # 先让旧连接回调失效，再调用SDK关闭。这样即使关闭期间仍有一条
            # 回调正在排队，也不会被标记成后续连接的订阅代次。
            self._callback_connection_generation += 1
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

        # 一次SQL批量预热并建立本订阅代次专用的双向代码映射；后续正常
        # Tick只执行内存字典查询，不再创建数据库Session。
        with self.session_factory() as db:
            self.market_data_service.refresh_instrument_cache(db, codes)
            code_mapping = self.code_mapping_service.build_snapshot(db, codes)
        with self._state_lock:
            self._code_mapping = code_mapping
            self._callback_generation = generation
            self._callback_connection_generation += 1
            connection_generation = self._callback_connection_generation

        def is_current_connection() -> bool:
            with self._state_lock:
                return (
                    connection_generation
                    == self._callback_connection_generation
                )

        def on_current_quote(data: dict[str, Any], raw: dict[str, Any]) -> None:
            if is_current_connection():
                self.on_quote(data, raw)

        def on_current_message(message: dict[str, Any]) -> None:
            if is_current_connection():
                self.on_message(message)

        def on_current_error(error: dict[str, Any]) -> None:
            if is_current_connection():
                self.on_error(error)

        subscription = self.feed_client.start_tick_callbacks(
            code_mapping.source_codes,
            on_quote=on_current_quote,
            on_subscribe=lambda report: self.on_subscribe(
                report,
                generation=self._callback_generation,
                code_mapping=self._code_mapping,
            ),
            on_message=on_current_message,
            on_error=on_current_error,
        )
        self._subscription = subscription
        if self._ever_started:
            self._increment("reconnect_count")
        self._ever_started = True
        with self._state_lock:
            self.last_reconnect_at = utc_now()

    def _replace_subscription(self, codes: frozenset[str]) -> None:
        """在同一SDK连接上批量增订/退订，并切换回调代次和代码映射。"""

        previous_codes = self.subscription_service.state_snapshot().requested_codes
        removed_codes = set(previous_codes) - set(codes)
        with self._state_lock:
            self._bootstrap_requested_codes.difference_update(removed_codes)
            for code in removed_codes:
                self._latest_live_event_times.pop(code, None)
                self._bootstrap_watermarks.pop(code, None)
                self._bootstrap_retry_at.pop(code, None)
        generation = self.subscription_service.mark_requested(codes)
        with self.session_factory() as db:
            self.market_data_service.refresh_instrument_cache(db, codes)
            code_mapping = self.code_mapping_service.build_snapshot(db, codes)
        with self._state_lock:
            self._code_mapping = code_mapping
            self._callback_generation = generation
            self._status = MarketDataSourceStatus.CONNECTING
        report = self.feed_client.replace_tick_subscriptions(
            code_mapping.source_codes
        )
        self.on_subscribe(
            report,
            generation=generation,
            code_mapping=code_mapping,
        )

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
                # start_tick_callbacks可能已经短暂启动过SDK线程；即使适配器关闭
                # 超时，失败连接的晚到回调也必须立即失效。
                self._callback_connection_generation += 1
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
        if self._source_issue:
            if self._source_issue.endswith(
                ("_DISCONNECTED", "_RECONNECTING")
            ):
                return MarketDataSourceStatus.DISCONNECTED
            return MarketDataSourceStatus.DEGRADED
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
                    "source": "YMM_LIVE_DATA",
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
            if not change.codes:
                self._stop_subscription()
                self.subscription_service.clear()
                with self._state_lock:
                    self._bootstrap_requested_codes.clear()
                    self._bootstrap_retry_at.clear()
                    self._latest_live_event_times.clear()
                    self._bootstrap_watermarks.clear()
                    self._reconnect_delay = self.reconnect_initial_seconds
                    self._next_reconnect_at = 0.0
                    self._retry_generation = None
                    self._disconnected_waiting = False
                    self.last_error = ""
                    self._status = MarketDataSourceStatus.IDLE
            else:
                # 官方SDK支持批量subscribe/unsubscribe。连接健康时增量调整
                # 频道；只有两个消费者线程已经退出才重建客户端。
                with self._state_lock:
                    self._reconnect_delay = self.reconnect_initial_seconds
                    self._next_reconnect_at = now
                    self._retry_generation = None
                if (
                    self._subscription is not None
                    and self._subscription.is_alive()
                ):
                    try:
                        self._replace_subscription(change.codes)
                    except Exception as exc:
                        with self._state_lock:
                            self.last_error = type(exc).__name__
                        self._mark_disconnected(now)
                        logger.warning(
                            "行情频道变更失败 error_type=%s",
                            type(exc).__name__,
                        )
                else:
                    self._try_start(change.codes, now)
        else:
            state = self.subscription_service.state_snapshot()
            if state.failed_codes and now >= self._next_reconnect_at:
                try:
                    self._replace_subscription(state.requested_codes)
                except Exception as exc:
                    self._mark_disconnected(now)
                    logger.warning(
                        "行情失败频道重试异常 error_type=%s",
                        type(exc).__name__,
                    )
            elif self._subscription is None and state.requested_codes:
                self._try_start(state.requested_codes, now)

        self._publish_source_status()
        state = self.subscription_service.state_snapshot()
        if state.subscribed_codes:
            self._schedule_bootstrap_snapshots(
                state.subscribed_codes,
                generation=state.generation,
                code_mapping=self._code_mapping,
            )

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
            self._snapshot_executor.shutdown(wait=True, cancel_futures=True)
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

    # 配置缺失应在进程启动阶段明确失败，不能静默保持IDLE或回退旧行情源。
    remote_sdk_client_kwargs(settings)
    load_remote_sdk_client_class()
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
    code_mapping_service = MarketDataCodeMappingService(
        InstrumentMarketDataMappingRepository()
    )
    subscription_service = MarketSubscriptionService(
        active_order_index=ActiveOrderIndex(redis_client),
        # 活动持仓合约索引由实时盈亏Worker维护在Redis中。复用该索引可让
        # 已成交持仓继续接收行情，同时避免订阅Worker高频查询PostgreSQL。
        active_position_contract_source=RealtimePnlStore(redis_client),
        # 下单前临时需求与活动订单、持仓订阅取并集。商品期权写入
        # “期权+标的期货”，股指期权写入“期权+标的指数”。
        pre_subscription_source=MarketPreSubscriptionStore(
            redis_client,
            ttl_seconds=settings.market_pre_subscription_ttl_seconds,
            max_codes_per_account=(
                settings.market_pre_subscription_max_codes_per_account
            ),
        ),
        client_subscription_source=ClientMarketSubscriptionStore(
            redis_client,
            ttl_seconds=settings.market_client_subscription_ttl_seconds,
            max_codes_per_connection=(
                settings.market_client_subscription_max_codes_per_connection
            ),
        ),
        debounce_seconds=(
            settings.remote_market_data_subscription_debounce_seconds
        ),
    )
    return MarketDataSubscriberWorker(
        session_factory=SessionLocal,
        feed_client=RemoteFeedClient(
            lambda: create_remote_sdk_client(settings)
        ),
        market_data_service=market_data_service,
        code_mapping_service=code_mapping_service,
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
        database_snapshot_client=YmmDatabaseSnapshotClient(settings),
        database_snapshot_retry_seconds=(
            settings.market_database_snapshot_retry_seconds
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
