from dataclasses import dataclass
from threading import RLock
from typing import Any

from sqlalchemy.orm import Session

from app.common.code_utils import normalize_code
from app.infrastructure.market_data.market_tick_store import (
    MarketTickStore,
    MarketTickStoreResult,
)
from app.repositories.instrument_repository import InstrumentRepository
from app.schemas.market_tick_schema import MarketTick
from app.services.market_tick_normalizer import MarketTickNormalizer
from app.services.market_tick_validation_service import (
    MarketTickValidationError,
    MarketTickValidationService,
)


@dataclass(frozen=True)
class MarketDataProcessResult:
    action: MarketTickStoreResult
    tick: MarketTick


@dataclass(frozen=True)
class MarketInstrumentSnapshot:
    """从ORM对象复制出的只读合约快照，可安全跨Session和线程使用。"""

    order_book_id: str
    exchange_id: str
    symbol: str
    is_active: bool


class MarketDataService:
    """协调合约查询、标准化、校验和Redis发布，不写PostgreSQL。"""

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
        # 缓存生命周期与订阅生命周期一致，不按时间过期，不在Tick路径反复查库。
        # None代表合约在最近一次订阅预热或首次查询时不存在，用于负缓存。
        self._instrument_cache: dict[
            str,
            MarketInstrumentSnapshot | None,
        ] = {}
        # 主线程会在订阅变化时预热，Tick消费线程同时读取，因此必须加锁。
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
        """返回(是否命中, 快照)；不存在的负缓存同样算命中。"""

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
        """
        订阅建立前用一次SQL批量预热全部合约，并为缺失合约写入负缓存。

        这里只更新传入编号，不清空其他仍可能被消费线程处理的缓存项。
        """

        normalized_ids = {normalize_code(code) for code in order_book_ids}
        instruments = self.instrument_repository.list_by_order_book_ids(
            db,
            normalized_ids,
        )
        by_id = {
            instrument.order_book_id: instrument
            for instrument in instruments
        }
        for order_book_id in normalized_ids:
            self._cache_instrument(
                order_book_id,
                by_id.get(order_book_id),
            )

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
    ) -> MarketDataProcessResult:
        if instrument is None:
            raise MarketTickValidationError("合约不存在")
        tick = self.normalizer.normalize(
            data=data,
            raw=raw,
            instrument=instrument,
        )
        self.validation_service.validate(tick=tick, instrument=instrument)
        return MarketDataProcessResult(
            action=self.tick_store.publish(tick),
            tick=tick,
        )

    def process(
        self,
        db: Session,
        *,
        data: dict[str, Any],
        raw: dict[str, Any],
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
        )

    def process_with_session_factory(
        self,
        session_factory,
        *,
        data: dict[str, Any],
        raw: dict[str, Any],
    ) -> MarketDataProcessResult:
        """
        Tick线程专用入口：缓存命中时完全不创建数据库Session。

        只有首次看到未预热合约时，才创建Session并执行一次按order_book_id
        查询。缓存不按时间过期，后续Tick全部走内存。
        """

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
        )
