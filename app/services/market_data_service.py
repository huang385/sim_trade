from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Any

from sqlalchemy.orm import Session

from app.common.code_utils import normalize_code
from app.enums.market_feed_enums import (
    MarketFeedDomain,
    resolve_market_feed_domain,
)
from app.infrastructure.market_data.market_tick_store import (
    MarketTickStore,
    MarketTickStoreResult,
)
from app.repositories.instrument_repository import InstrumentRepository
from app.schemas.market_tick_schema import MarketTick, MarketTickIngestType
from app.services.market_tick_normalizer import MarketTickNormalizer
from app.services.market_tick_validation_service import (
    MarketTickValidationError,
    MarketTickValidationService,
)


class MarketDataProcessAction(str, Enum):
    """行情业务处理结果。"""

    PUBLISHED = "PUBLISHED"


@dataclass(frozen=True)
class MarketDataProcessResult:
    action: MarketDataProcessAction
    tick: MarketTick


@dataclass(frozen=True)
class MarketInstrumentSnapshot:
    """从 ORM 对象复制出的只读合约快照，可安全跨 Session 和线程使用。"""

    order_book_id: str
    exchange_id: str
    symbol: str
    is_active: bool
    instrument_type: str


class MarketDataService:
    """协调合约查询、标准化、必要校验和 WebSocket 行情发布。"""

    def __init__(
        self,
        *,
        instrument_repository: InstrumentRepository,
        normalizer: MarketTickNormalizer,
        validation_service: MarketTickValidationService,
        tick_store: MarketTickStore,
        market_domain: MarketFeedDomain,
    ):
        self.instrument_repository = instrument_repository
        self.normalizer = normalizer
        self.validation_service = validation_service
        self.tick_store = tick_store
        self.market_domain = market_domain

        # 缓存与 Worker 生命周期一致，不按时间过期；None 表示负缓存。
        self._instrument_cache: dict[str, MarketInstrumentSnapshot | None] = {}
        self._instrument_cache_lock = RLock()

    @staticmethod
    def _snapshot(instrument) -> MarketInstrumentSnapshot:
        return MarketInstrumentSnapshot(
            order_book_id=instrument.order_book_id,
            exchange_id=instrument.exchange_id,
            symbol=instrument.symbol,
            is_active=bool(instrument.is_active),
            instrument_type=str(
                getattr(instrument.instrument_type, "value", instrument.instrument_type)
            ),
        )

    def _get_cached_instrument(
        self,
        order_book_id: str,
    ) -> tuple[bool, MarketInstrumentSnapshot | None]:
        with self._instrument_cache_lock:
            if order_book_id not in self._instrument_cache:
                return False, None
            return True, self._instrument_cache[order_book_id]

    def _cache_instrument(
        self,
        order_book_id: str,
        instrument,
    ) -> MarketInstrumentSnapshot | None:
        snapshot = self._snapshot(instrument) if instrument is not None else None
        with self._instrument_cache_lock:
            self._instrument_cache[order_book_id] = snapshot
        return snapshot

    def refresh_instrument_cache(
        self,
        db: Session,
        order_book_ids: set[str] | frozenset[str] | list[str],
    ) -> None:
        """订阅建立前用一次 SQL 批量预热合约和负缓存。"""

        normalized_ids = {normalize_code(code) for code in order_book_ids}
        instruments = self.instrument_repository.list_by_order_book_ids(
            db,
            normalized_ids,
        )
        by_id = {instrument.order_book_id: instrument for instrument in instruments}
        for order_book_id in normalized_ids:
            self._cache_instrument(order_book_id, by_id.get(order_book_id))

    def _load_instrument(
        self,
        db: Session,
        order_book_id: str,
    ) -> MarketInstrumentSnapshot | None:
        instrument = self.instrument_repository.get_by_order_book_id(
            db,
            order_book_id,
        )
        return self._cache_instrument(order_book_id, instrument)

    def _process_with_instrument(
        self,
        *,
        data: dict[str, Any],
        raw: dict[str, Any],
        instrument: MarketInstrumentSnapshot | None,
        ingest_type: MarketTickIngestType,
        source: str,
        subscription_generation: int | None = None,
    ) -> MarketDataProcessResult:
        if instrument is None:
            raise MarketTickValidationError("合约不存在")
        if resolve_market_feed_domain(instrument.instrument_type) != self.market_domain:
            raise MarketTickValidationError(
                f"合约不属于当前行情域: {self.market_domain.value}"
            )
        tick = self.normalizer.normalize(
            data=data,
            raw=raw,
            instrument=instrument,
            ingest_type=ingest_type,
            source=source,
        )
        self.validation_service.validate(tick=tick, instrument=instrument)

        store_result = self.tick_store.publish(
            tick,
            subscription_generation=subscription_generation,
        )
        if store_result == MarketTickStoreResult.IGNORED_STALE:
            # 晚到快照发现 Redis 已有更可靠行情时不写新事件；这是正常的
            # 竞争结果，调用方随后会从 Redis 读取最终胜出的行情。
            return MarketDataProcessResult(
                action=MarketDataProcessAction.PUBLISHED,
                tick=tick,
            )
        if store_result != MarketTickStoreResult.PUBLISHED:
            raise RuntimeError(f"未知行情存储结果: {store_result}")
        return MarketDataProcessResult(action=MarketDataProcessAction.PUBLISHED, tick=tick)

    def process(
        self,
        db: Session,
        *,
        data: dict[str, Any],
        raw: dict[str, Any],
        ingest_type: MarketTickIngestType = MarketTickIngestType.LIVE_CALLBACK,
        source: str = "YMM_LIVE_DATA",
        subscription_generation: int | None = None,
    ) -> MarketDataProcessResult:
        self.validation_service.validate_envelope(data=data, raw=raw)
        order_book_id = normalize_code(
            str(data.get("order_book_id") or "")
        )
        cache_hit, instrument = self._get_cached_instrument(order_book_id)
        if not cache_hit:
            instrument = self._load_instrument(db, order_book_id)
        return self._process_with_instrument(
            data=data,
            raw=raw,
            instrument=instrument,
            ingest_type=ingest_type,
            source=source,
            subscription_generation=subscription_generation,
        )

    def process_with_session_factory(
        self,
        session_factory,
        *,
        data: dict[str, Any],
        raw: dict[str, Any],
        ingest_type: MarketTickIngestType = MarketTickIngestType.LIVE_CALLBACK,
        source: str = "YMM_LIVE_DATA",
        subscription_generation: int | None = None,
    ) -> MarketDataProcessResult:
        """Tick 线程入口：缓存命中时完全不创建数据库 Session。"""

        self.validation_service.validate_envelope(data=data, raw=raw)
        order_book_id = normalize_code(
            str(data.get("order_book_id") or "")
        )
        cache_hit, instrument = self._get_cached_instrument(order_book_id)
        if not cache_hit:
            with session_factory() as db:
                instrument = self._load_instrument(db, order_book_id)
        return self._process_with_instrument(
            data=data,
            raw=raw,
            instrument=instrument,
            ingest_type=ingest_type,
            source=source,
            subscription_generation=subscription_generation,
        )
