import logging
import os
import signal
import socket
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from threading import Event, Lock
from typing import Callable

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.logging_config import setup_logging
from app.core.redis_client import redis_client
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.market_tick_stream_consumer import (
    MarketStreamMessage,
    MarketTickStreamConsumer,
)
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.infrastructure.redis_keys import pnl_event_failure_key
from app.schemas.market_tick_schema import MarketTick
from app.services.active_position_cache import ActivePositionCache
from app.services.realtime_pnl_service import (
    ContractPnlRequest,
    PnlEventValidationError,
    PnlWorkerLeaseLostError,
    RealtimePnlService,
)


logger = logging.getLogger(__name__)
ContractKey = tuple[str, str]


def generate_consumer_name() -> str:
    return f"pnl-consumer-{socket.gethostname()}-{os.getpid()}"


def _stream_id_order(message_id: str) -> tuple[int, int]:
    """Redis Stream ID必须按毫秒和序号两个整数比较，不能比较字符串。"""

    milliseconds, sequence = message_id.split("-", 1)
    return int(milliseconds), int(sequence)


@dataclass
class BufferedContract:
    """一个500ms窗口内单合约的最新行情及全部待确认消息。"""

    latest_message_id: str
    latest_tick: MarketTick
    message_ids: list[str]
    fields_by_id: dict[str, dict[str, str]]


@dataclass(frozen=True)
class PnlWorkerStats:
    """PnL Worker累计运行统计；不逐Tick打印日志。"""

    ticks_read: int = 0
    ticks_coalesced: int = 0
    contracts_flushed: int = 0
    contracts_skipped_unchanged: int = 0
    contracts_dirty_by_trade: int = 0
    positions_calculated: int = 0
    accounts_updated: int = 0
    redis_batches_written: int = 0
    messages_acked: int = 0
    pending_retained: int = 0
    dead_lettered: int = 0
    cache_refresh_count: int = 0
    full_reconciliation_count: int = 0
    calculation_duration_ms: int = 0
    failed_ticks: int = 0


class RealtimePnlWorker:
    """
    单实例消费行情，并每500ms按交易所+合约合并计算实时盈亏。

    撮合使用独立Consumer Group，仍逐Tick处理。本Worker只降低派生PnL快照
    的刷新频率，不改变成交、订单或资金事实。
    """

    def __init__(
        self,
        *,
        stream_consumer: MarketTickStreamConsumer,
        service: RealtimePnlService,
        pnl_store: RealtimePnlStore | None = None,
        market_tick_store: MarketTickStore | None = None,
        batch_size: int,
        block_ms: int,
        pending_idle_ms: int,
        max_retries: int,
        retry_interval_seconds: float,
        calculation_interval_ms: int = 500,
        full_reconciliation_interval_seconds: int = 60,
        monotonic: Callable[[], float] = time.monotonic,
        lease_owner: str | None = None,
        lease_ttl_seconds: int = 15,
        lease_renew_seconds: int = 5,
    ):
        self.stream_consumer = stream_consumer
        self.service = service
        self.pnl_store = pnl_store or service.pnl_store
        self.market_tick_store = market_tick_store
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.pending_idle_ms = pending_idle_ms
        self.max_retries = max_retries
        self.retry_interval_seconds = retry_interval_seconds
        self.calculation_interval_seconds = (
            max(calculation_interval_ms, 1) / 1000
        )
        self.reconciliation_interval_seconds = max(
            full_reconciliation_interval_seconds,
            1,
        )
        self.monotonic = monotonic
        now = self.monotonic()
        self._next_flush_at = now + self.calculation_interval_seconds
        # 启动首轮立即完整恢复，之后再按低频周期对账。
        self._next_reconciliation_at = now
        self._next_stats_log_at = now + 30
        self._buffer: dict[ContractKey, BufferedContract] = {}
        self._last_successful_prices: dict[ContractKey, Decimal] = {}
        self._last_cache_refresh_count = 0
        self._indexes_rebuilt = False
        self.stop_event = Event()
        self._stats = PnlWorkerStats()
        self._stats_lock = Lock()
        self.lease_owner = lease_owner or generate_consumer_name()
        self.lease_ttl_seconds = lease_ttl_seconds
        self.lease_renew_seconds = lease_renew_seconds
        self._next_lease_renew_at = now

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def stats_snapshot(self) -> PnlWorkerStats:
        with self._stats_lock:
            return self._stats

    def _update_stats(self, **changes: int) -> None:
        with self._stats_lock:
            values = {
                name: getattr(self._stats, name) + value
                for name, value in changes.items()
            }
            self._stats = replace(self._stats, **values)

    def _ack_many(self, message_ids: list[str]) -> int:
        ids = list(dict.fromkeys(message_ids))
        if not ids:
            return 0
        acknowledged = self.stream_consumer.acknowledge_many(ids)
        self.stream_consumer.clear_failures(ids)
        self._update_stats(messages_acked=acknowledged)
        return acknowledged

    def _dead_letter(
        self,
        message_id: str,
        fields: dict[str, str],
        error: str,
    ) -> None:
        self.stream_consumer.publish_dead_letter(
            source_message_id=message_id,
            fields=fields,
            error=error,
        )
        self.stream_consumer.acknowledge(message_id)
        self.stream_consumer.clear_failure(message_id)
        self._update_stats(dead_lettered=1, messages_acked=1)

    def _buffer_message(
        self,
        message_id: str,
        fields: dict[str, str] | None,
    ) -> str:
        if fields is None:
            self._ack_many([message_id])
            return "acknowledged"
        self._update_stats(ticks_read=1)
        try:
            tick = self.service.parse_tick(fields)
        except PnlEventValidationError as exc:
            self._dead_letter(message_id, fields, str(exc))
            return "dead_lettered"
        if tick is None or tick.last_price is None or tick.last_price <= 0:
            self._ack_many([message_id])
            return "acknowledged"

        key = (
            tick.exchange_id.strip().upper(),
            tick.symbol.strip().upper(),
        )
        buffered = self._buffer.get(key)
        if buffered is None:
            self._buffer[key] = BufferedContract(
                latest_message_id=message_id,
                latest_tick=tick,
                message_ids=[message_id],
                fields_by_id={message_id: fields},
            )
            return "buffered"
        if message_id not in buffered.message_ids:
            buffered.message_ids.append(message_id)
            buffered.fields_by_id[message_id] = fields
            self._update_stats(ticks_coalesced=1)
        if _stream_id_order(message_id) > _stream_id_order(
            buffered.latest_message_id
        ):
            buffered.latest_message_id = message_id
            buffered.latest_tick = tick
        return "buffered"

    def _renew_current_lease(self) -> bool:
        """
        向Redis确认当前实例仍是唯一写入者并刷新TTL。

        Redis异常由调用方按失败关闭处理；返回False时本轮不得写快照或ACK。
        """

        renewed = self.pnl_store.renew_worker_lease(
            self.lease_owner,
            self.lease_ttl_seconds,
        )
        if renewed:
            self._next_lease_renew_at = (
                self.monotonic() + self.lease_renew_seconds
            )
        return renewed

    def _current_ticks(
        self,
        keys: set[ContractKey],
    ) -> dict[ContractKey, MarketTick | None]:
        """
        一次Pipeline读取本周期全部最新行情。

        旧Pending只作为触发器，必须采用market:latest当前价格；Redis Hash不可用
        时，正常新消息仍可退回本周期已经解析过的最新Tick。
        """

        latest_rows = (
            self.market_tick_store.get_latest_many(keys)
            if self.market_tick_store is not None
            else {}
        )
        result: dict[ContractKey, MarketTick | None] = {}
        for key in sorted(keys):
            latest = latest_rows.get(key, {})
            if latest:
                try:
                    tick = MarketTickStore.mapping_to_tick(latest)
                    if tick.last_price is not None and tick.last_price > 0:
                        result[key] = tick
                        continue
                except Exception:
                    logger.warning(
                        "PnL最新行情快照格式无效 contract=%s:%s",
                        *key,
                    )
            buffered = self._buffer.get(key)
            result[key] = (
                buffered.latest_tick if buffered is not None else None
            )
        return result

    def _retain_or_dead_letter(
        self,
        key: ContractKey,
        error: str,
    ) -> None:
        """记录合约批次失败；达到上限的原消息按既有规则进入死信。"""

        buffered = self._buffer.get(key)
        if buffered is None:
            return
        retained_ids: list[str] = []
        for message_id in buffered.message_ids:
            failures = self.stream_consumer.increment_failure(message_id)
            if failures >= self.max_retries:
                self._dead_letter(
                    message_id,
                    buffered.fields_by_id[message_id],
                    error,
                )
                buffered.fields_by_id.pop(message_id, None)
            else:
                retained_ids.append(message_id)
        buffered.message_ids = retained_ids
        if not retained_ids:
            self._buffer.pop(key, None)

    def _schedule_next_flush(self, now: float) -> None:
        # 以原时间线推进；若计算已经超期，下一轮会立即执行而不是再睡500ms。
        self._next_flush_at += self.calculation_interval_seconds
        if (
            self._next_flush_at < now - self.calculation_interval_seconds
        ):
            self._next_flush_at = now

    def flush(self, *, force_reconciliation: bool = False) -> None:
        if not self._renew_current_lease():
            logger.warning("实时PnL租约已失效，本轮不写入且保留Pending")
            return
        started = self.monotonic()
        dirty_rows = self.pnl_store.list_dirty_contracts()
        dirty = {key: (version, accounts) for key, version, accounts in dirty_rows}
        keys = set(self._buffer) | set(dirty)

        extra_accounts = {
            account_id
            for _version, accounts in dirty.values()
            for account_id in accounts
        }
        cycle = self.service.active_position_cache.get_cycle_snapshot(
            extra_account_ids=extra_accounts,
            force_refresh=force_reconciliation,
        )
        refresh_delta = max(
            cycle.refresh_count - self._last_cache_refresh_count,
            0,
        )
        self._last_cache_refresh_count = cycle.refresh_count
        if refresh_delta:
            self._update_stats(cache_refresh_count=refresh_delta)

        if force_reconciliation:
            keys.update(cycle.by_contract.keys())
        if not keys:
            if not self._indexes_rebuilt:
                self._indexes_rebuilt = (
                    self.pnl_store.rebuild_active_indexes(
                        expected_cache_version=(
                            cycle.cache_version or "0"
                        ),
                        positions=[
                            (
                                position.account_id,
                                position.exchange_id,
                                position.symbol,
                                position.position_id,
                            )
                            for positions in cycle.by_contract.values()
                            for position in positions
                        ],
                    )
                )
            self._schedule_next_flush(self.monotonic())
            return

        requests: list[ContractPnlRequest] = []
        skipped_unchanged: set[ContractKey] = set()
        current_ticks = self._current_ticks(keys)
        for key in sorted(keys):
            buffered = self._buffer.get(key)
            tick = current_ticks.get(key)
            dirty_version, dirty_accounts = dirty.get(
                key,
                (None, set()),
            )
            price = tick.last_price if tick is not None else None
            if (
                not force_reconciliation
                and dirty_version is None
                and self._last_successful_prices.get(key) == price
            ):
                skipped_unchanged.add(key)
                continue
            requests.append(
                ContractPnlRequest(
                    exchange_id=key[0],
                    symbol=key[1],
                    tick=tick,
                    dirty_version=dirty_version,
                    dirty_account_ids=frozenset(dirty_accounts),
                )
            )

        if skipped_unchanged and not self._renew_current_lease():
            logger.warning(
                "实时PnL租约在确认未变化行情前失效，消息保留Pending"
            )
            self._schedule_next_flush(self.monotonic())
            return
        for key in skipped_unchanged:
            buffered = self._buffer.get(key)
            if buffered is not None:
                self._ack_many(buffered.message_ids)
                self._buffer.pop(key, None)
        if skipped_unchanged:
            self._update_stats(
                contracts_skipped_unchanged=len(skipped_unchanged)
            )

        if not requests:
            self._schedule_next_flush(self.monotonic())
            return

        try:
            result = self.service.process_batch(
                requests=requests,
                cycle_snapshot=cycle,
                dirty_version=f"cycle:{int(started * 1000)}",
                force_reconciliation=force_reconciliation,
                lease_owner=self.lease_owner,
            )
        except PnlWorkerLeaseLostError:
            retained = sum(
                len(item.message_ids) for item in self._buffer.values()
            )
            self._update_stats(pending_retained=retained)
            logger.warning(
                "实时PnL最终写入被租约屏障拒绝，消息保留Pending"
            )
            self._schedule_next_flush(self.monotonic())
            return
        except Exception:
            for key in list(self._buffer):
                self._retain_or_dead_letter(
                    key,
                    "PnL周期批量写入失败",
                )
            retained = sum(
                len(item.message_ids) for item in self._buffer.values()
            )
            self._update_stats(
                pending_retained=retained,
                failed_ticks=retained,
            )
            logger.exception("PnL周期批量写入失败，全部消息保留Pending")
            self._schedule_next_flush(self.monotonic())
            return

        # 最终快照已经通过Lua租约屏障写入；ACK前再次续租。若此时失去租约，
        # 保留原消息由新Worker幂等重算，不更新任何本地成功状态。
        if not self._renew_current_lease():
            retained = sum(
                len(item.message_ids) for item in self._buffer.values()
            )
            self._update_stats(pending_retained=retained)
            logger.warning(
                "实时PnL写入后租约失效，本轮不ACK并交由新Worker恢复"
            )
            self._schedule_next_flush(self.monotonic())
            return

        request_by_key = {request.key: request for request in requests}
        successful = set(result.successful_contracts)
        for key in successful:
            request = request_by_key[key]
            if request.tick is not None:
                self._last_successful_prices[key] = request.tick.last_price
            buffered = self._buffer.get(key)
            if buffered is not None:
                self._ack_many(buffered.message_ids)
                self._buffer.pop(key, None)
            if request.dirty_version is not None:
                self.pnl_store.complete_dirty_contract(
                    exchange_id=key[0],
                    symbol=key[1],
                    expected_version=request.dirty_version,
                )

        for key in result.failed_contracts:
            self._retain_or_dead_letter(
                key,
                "合约实时PnL计算失败",
            )

        failed_message_count = sum(
            len(self._buffer[key].message_ids)
            for key in result.failed_contracts
            if key in self._buffer
        )
        duration_ms = int(
            max(self.monotonic() - started, 0) * 1000
        )
        self._update_stats(
            contracts_flushed=len(successful),
            contracts_dirty_by_trade=sum(
                1 for item in requests if item.dirty_version is not None
            ),
            positions_calculated=result.positions_calculated,
            accounts_updated=result.accounts_updated,
            redis_batches_written=(
                1 if result.redis_snapshots_written else 0
            ),
            pending_retained=failed_message_count,
            full_reconciliation_count=(
                1 if force_reconciliation else 0
            ),
            calculation_duration_ms=duration_ms,
        )
        if not self._indexes_rebuilt:
            self._indexes_rebuilt = self.pnl_store.rebuild_active_indexes(
                expected_cache_version=(cycle.cache_version or "0"),
                positions=[
                    (
                        position.account_id,
                        position.exchange_id,
                        position.symbol,
                        position.position_id,
                    )
                    for positions in cycle.by_contract.values()
                    for position in positions
                ],
            )
        self._schedule_next_flush(self.monotonic())

    def _read_block_ms(self) -> int:
        now = self.monotonic()
        remaining_ms = int(max(self._next_flush_at - now, 0) * 1000)
        return min(self.block_ms, max(1, remaining_ms))

    def run_once(self, *, force_flush: bool = False) -> None:
        if not self._renew_current_lease():
            logger.warning("实时PnL租约已失效，本轮不读取或确认行情")
            return
        messages = self.stream_consumer.claim_stale_messages(
            pending_idle_ms=self.pending_idle_ms,
            batch_size=self.batch_size,
        )
        messages += self.stream_consumer.read_new_messages(
            batch_size=self.batch_size,
            block_ms=self._read_block_ms(),
        )
        for message_id, fields in messages:
            self._buffer_message(message_id, fields)

        now = self.monotonic()
        reconcile = now >= self._next_reconciliation_at
        if force_flush or now >= self._next_flush_at or reconcile:
            self.flush(force_reconciliation=reconcile)
            if reconcile:
                self._next_reconciliation_at = (
                    now + self.reconciliation_interval_seconds
                )
        if now >= self._next_stats_log_at:
            logger.info("PnL Worker周期统计 %s", self.stats_snapshot())
            self._next_stats_log_at = now + 30

    def run_forever(self) -> None:
        group_ready = False
        lease_acquired = False
        try:
            while not self.stop_event.is_set():
                try:
                    now = self.monotonic()
                    if not lease_acquired:
                        lease_acquired = self.pnl_store.acquire_worker_lease(
                            self.lease_owner,
                            self.lease_ttl_seconds,
                        )
                        if not lease_acquired:
                            logger.warning(
                                "已有实时PnL单写者，本实例等待租约"
                            )
                            self.stop_event.wait(
                                self.retry_interval_seconds
                            )
                            continue
                        self._next_lease_renew_at = (
                            now + self.lease_renew_seconds
                        )
                    elif now >= self._next_lease_renew_at:
                        if not self.pnl_store.renew_worker_lease(
                            self.lease_owner,
                            self.lease_ttl_seconds,
                        ):
                            lease_acquired = False
                            raise RuntimeError("实时PnL单写者租约已丢失")
                        self._next_lease_renew_at = (
                            now + self.lease_renew_seconds
                        )
                    if not group_ready:
                        self.stream_consumer.ensure_group()
                        group_ready = True
                        logger.info(
                            "PnL Consumer Group已就绪 stream=%s group=%s "
                            "consumer=%s interval_ms=%s",
                            self.stream_consumer.stream_name,
                            self.stream_consumer.group_name,
                            self.stream_consumer.consumer_name,
                            int(self.calculation_interval_seconds * 1000),
                        )
                    self.run_once()
                except Exception:
                    logger.exception("PnL行情消费循环异常")
                    self.stop_event.wait(self.retry_interval_seconds)
        finally:
            try:
                if self._buffer and self._renew_current_lease():
                    self.flush()
            except Exception:
                # Redis不可用或无法确认租约时采用失败关闭，不执行退出写入。
                logger.exception(
                    "实时PnL退出时无法确认租约，已放弃刷新并保留Pending"
                )
            if lease_acquired:
                self.pnl_store.release_worker_lease(self.lease_owner)


def build_worker() -> RealtimePnlWorker:
    pnl_store = RealtimePnlStore(redis_client)
    market_tick_store = MarketTickStore(
        redis_client,
        stream_name=settings.market_tick_stream_name,
    )
    consumer = MarketTickStreamConsumer(
        redis_client,
        stream_name=settings.market_tick_stream_name,
        group_name=settings.pnl_consumer_group,
        consumer_name=(
            settings.pnl_consumer_name or generate_consumer_name()
        ),
        dead_letter_stream=settings.pnl_dead_letter_stream,
        failure_ttl_seconds=settings.pnl_failure_ttl_seconds,
        failure_key_factory=pnl_event_failure_key,
    )
    cache = ActivePositionCache(
        session_factory=SessionLocal,
        refresh_ms=settings.active_position_cache_refresh_ms,
        version_loader=pnl_store.get_position_cache_version,
    )
    service = RealtimePnlService(
        active_position_cache=cache,
        pnl_store=pnl_store,
        market_tick_store=market_tick_store,
    )
    return RealtimePnlWorker(
        stream_consumer=consumer,
        service=service,
        pnl_store=pnl_store,
        market_tick_store=market_tick_store,
        batch_size=settings.pnl_consumer_batch_size,
        block_ms=settings.pnl_consumer_block_ms,
        pending_idle_ms=settings.pnl_pending_idle_ms,
        max_retries=settings.pnl_event_max_retries,
        retry_interval_seconds=(
            settings.pnl_consumer_retry_interval_seconds
        ),
        calculation_interval_ms=settings.pnl_calculation_interval_ms,
        full_reconciliation_interval_seconds=(
            settings.pnl_full_reconciliation_interval_seconds
        ),
        lease_ttl_seconds=settings.pnl_worker_lease_ttl_seconds,
        lease_renew_seconds=settings.pnl_worker_lease_renew_seconds,
    )


def main() -> None:
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
