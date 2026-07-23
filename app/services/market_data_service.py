from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Any

from sqlalchemy.orm import Session

from app.common.code_utils import normalize_code
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
    """行情业务处理结果；REST 快照不会进入实时撮合 Stream。"""

    PUBLISHED = "PUBLISHED"
    REST_IGNORED = "REST_IGNORED"


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


class MarketDataService:
    """协调合约查询、标准化、必要校验和 WebSocket 行情发布。"""

    def __init__(
        self,
        *,
        instrument_repository: InstrumentRepository,
        normalizer: MarketTickNormalizer,
        validation_service: MarketTickValidationService,
        tick_store: MarketTickStore,
    ):
        self.instrument_repository = instrument_repository
        self.normalizer = normalizer
        self.validation_service = validation_service
        self.tick_store = tick_store

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
    ) -> MarketDataProcessResult:
        if instrument is None:
            raise MarketTickValidationError("合约不存在")
        tick = self.normalizer.normalize(
            data=data,
            raw=raw,
            instrument=instrument,
            ingest_type=ingest_type,
        )
        self.validation_service.validate(tick=tick, instrument=instrument)

        if ingest_type == MarketTickIngestType.REST_SNAPSHOT:
            # REST 只用于订阅启动阶段观察行情源是否有快照，不得触发撮合。
            return MarketDataProcessResult(
                action=MarketDataProcessAction.REST_IGNORED,
                tick=tick,
            )

        store_result = self.tick_store.publish(tick)
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
    ) -> MarketDataProcessResult:
        self.validation_service.validate_envelope(data=data, raw=raw)
        order_book_id = normalize_code(str(data.get("code") or ""))
        cache_hit, instrument = self._get_cached_instrument(order_book_id)
        if not cache_hit:
            instrument = self._load_instrument(db, order_book_id)
        return self._process_with_instrument(
            data=data,
            raw=raw,
            instrument=instrument,
            ingest_type=ingest_type,
        )

    def process_with_session_factory(
        self,
        session_factory,
        *,
        data: dict[str, Any],
        raw: dict[str, Any],
        ingest_type: MarketTickIngestType = MarketTickIngestType.LIVE_CALLBACK,
    ) -> MarketDataProcessResult:
        """Tick 线程入口：缓存命中时完全不创建数据库 Session。"""

        self.validation_service.validate_envelope(data=data, raw=raw)
        order_book_id = normalize_code(str(data.get("code") or ""))
        cache_hit, instrument = self._get_cached_instrument(order_book_id)
        if not cache_hit:
            with session_factory() as db:
                instrument = self._load_instrument(db, order_book_id)
        return self._process_with_instrument(
            data=data,
            raw=raw,
            instrument=instrument,
            ingest_type=ingest_type,
        )
