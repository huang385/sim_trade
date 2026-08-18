import json
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.common.code_utils import normalize_code
from app.common.exceptions import BusinessRuleError
from app.enums.order_enums import OffsetFlag
from app.models.instrument import Instrument
from app.repositories.trading_day_repository import TradingDayRepository


SHANGHAI = ZoneInfo("Asia/Shanghai")
ScheduleKey = tuple[str, str, str]


@dataclass(frozen=True)
class TradingSession:
    trading_day: date
    start_at: datetime
    end_at: datetime
    allow_open: bool
    allow_close: bool


@dataclass(frozen=True)
class CachedSchedule:
    sessions: tuple[TradingSession, ...]
    expires_at: float


class TradingDayService:
    """根据交易所日历和产品时段解析订单所属交易日。"""

    def __init__(
        self,
        *,
        repository: TradingDayRepository | None = None,
        cache_ttl_seconds: int = 300,
        now_provider: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repository = repository or TradingDayRepository()
        self.cache_ttl_seconds = max(int(cache_ttl_seconds), 1)
        self.now_provider = now_provider or (
            lambda: datetime.now(tz=SHANGHAI)
        )
        self.monotonic = monotonic
        self._cache: dict[ScheduleKey, CachedSchedule] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _parse_datetime(value: object) -> datetime:
        result = datetime.fromisoformat(str(value))
        if result.tzinfo is None:
            return result.replace(tzinfo=SHANGHAI)
        return result.astimezone(SHANGHAI)

    def _load(
        self,
        db: Session,
        *,
        key: ScheduleKey,
        now: datetime,
    ) -> CachedSchedule:
        rows = self.repository.list_candidate_schedules(
            db,
            exchange_id=key[0],
            product_code=key[1],
            instrument_type=key[2],
            start_day=now.date() - timedelta(days=1),
            end_day=now.date() + timedelta(days=14),
        )
        sessions: list[TradingSession] = []
        for row in rows:
            if not row.get("calendar_is_open"):
                continue
            if str(row.get("calendar_status") or "").upper() != "OPEN":
                continue
            if str(row.get("schedule_status") or "").upper() not in {
                "OPEN",
                "READY",
            }:
                continue
            raw_sessions = row.get("sessions")
            if isinstance(raw_sessions, str):
                raw_sessions = json.loads(raw_sessions)
            if not isinstance(raw_sessions, list):
                raise BusinessRuleError(
                    "品种交易时段格式不合法",
                    error_code="TRADING_SCHEDULE_INVALID",
                )
            for raw in raw_sessions:
                try:
                    start_at = self._parse_datetime(raw["start_at"])
                    end_at = self._parse_datetime(raw["end_at"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise BusinessRuleError(
                        "品种交易时段格式不合法",
                        error_code="TRADING_SCHEDULE_INVALID",
                    ) from exc
                if end_at <= start_at:
                    raise BusinessRuleError(
                        "品种交易时段起止时间不合法",
                        error_code="TRADING_SCHEDULE_INVALID",
                    )
                sessions.append(
                    TradingSession(
                        trading_day=row["trading_day"],
                        start_at=start_at,
                        end_at=end_at,
                        allow_open=bool(raw.get("allow_open", True)),
                        allow_close=bool(raw.get("allow_close", True)),
                    )
                )
        return CachedSchedule(
            sessions=tuple(sessions),
            expires_at=self.monotonic() + self.cache_ttl_seconds,
        )

    def _get_schedule(
        self,
        db: Session,
        *,
        key: ScheduleKey,
        now: datetime,
    ) -> CachedSchedule:
        current = self.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at > current:
                return cached
            loaded = self._load(db, key=key, now=now)
            self._cache[key] = loaded
            return loaded

    def resolve_for_order(
        self,
        db: Session,
        *,
        instrument: Instrument,
        offset_flag: OffsetFlag | str,
        now: datetime | None = None,
    ) -> date:
        exchange_id = normalize_code(instrument.exchange_id)
        product_code = normalize_code(str(instrument.product_id or ""))
        instrument_type = normalize_code(instrument.instrument_type)
        if not product_code:
            raise BusinessRuleError(
                "合约缺少品种代码，无法确定交易日",
                error_code="TRADING_DAY_CONTEXT_MISSING",
            )
        local_now = now or self.now_provider()
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=SHANGHAI)
        else:
            local_now = local_now.astimezone(SHANGHAI)
        schedule = self._get_schedule(
            db,
            key=(exchange_id, product_code, instrument_type),
            now=local_now,
        )
        is_open_order = OffsetFlag(offset_flag) == OffsetFlag.OPEN
        matches = {
            item.trading_day
            for item in schedule.sessions
            if item.start_at <= local_now < item.end_at
            and (item.allow_open if is_open_order else item.allow_close)
        }
        if len(matches) > 1:
            raise BusinessRuleError(
                "交易时段映射到多个交易日",
                error_code="TRADING_DAY_AMBIGUOUS",
            )
        if not matches:
            if not schedule.sessions:
                raise BusinessRuleError(
                    "当前品种缺少有效交易日历或交易时段",
                    error_code="TRADING_SCHEDULE_MISSING",
                )
            raise BusinessRuleError(
                "当前不在该品种允许下单的交易时段",
                error_code="OUTSIDE_TRADING_SESSION",
            )
        return next(iter(matches))

    def resolve_for_cash_security_order(
        self,
        db: Session,
        *,
        instrument: Instrument,
        now: datetime | None = None,
    ) -> date:
        """解析现金证券的可下单交易日，不引入开平仓标志。"""

        exchange_id = normalize_code(instrument.exchange_id)
        product_code = normalize_code(str(instrument.product_id or ""))
        instrument_type = normalize_code(instrument.instrument_type)
        if not product_code:
            raise BusinessRuleError(
                "合约缺少品种代码，无法确定交易日",
                error_code="TRADING_DAY_CONTEXT_MISSING",
            )
        local_now = now or self.now_provider()
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=SHANGHAI)
        else:
            local_now = local_now.astimezone(SHANGHAI)
        schedule = self._get_schedule(
            db,
            key=(exchange_id, product_code, instrument_type),
            now=local_now,
        )
        matches = {
            item.trading_day
            for item in schedule.sessions
            if item.start_at <= local_now < item.end_at and item.allow_open
        }
        if len(matches) > 1:
            raise BusinessRuleError(
                "交易时段映射到多个交易日",
                error_code="TRADING_DAY_AMBIGUOUS",
            )
        if not matches:
            if not schedule.sessions:
                raise BusinessRuleError(
                    "当前品种缺少有效交易日历或交易时段",
                    error_code="TRADING_SCHEDULE_MISSING",
                )
            raise BusinessRuleError(
                "当前不在该品种允许下单的交易时段",
                error_code="OUTSIDE_TRADING_SESSION",
            )
        return next(iter(matches))

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()


@lru_cache(maxsize=1)
def get_trading_day_service() -> TradingDayService:
    """返回进程级共享实例，使不同 HTTP 请求复用时段缓存。"""

    return TradingDayService()
