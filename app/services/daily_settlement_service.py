import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, Connection, select, text
from sqlalchemy.orm import Session

from app.common.decimal_utils import quantize_money
from app.common.exceptions import AppError, DataAccessError
from app.common.time_utils import utc_now
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.redis_client import redis_client
from app.enums.daily_settlement_enums import (
    DailySettlementAccountStatus,
    DailySettlementBatchStatus,
    DailySettlementStage,
    SettlementCacheStatus,
)
from app.enums.option_enums import InstrumentType, MarginPriceMode, OptionType
from app.enums.order_enums import PositionDetailStatus, PositionDirection
from app.infrastructure.active_order_index import ActiveOrderIndex
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.models.daily_settlement import (
    DailyAccountSettlement,
    DailyPositionSettlement,
    DailySettlementBatch,
    InstrumentSettlementPrice,
    OptionExpirySettlementDetail,
)
from app.models.account import Account
from app.models.instrument import Instrument
from app.models.margin_rule_daily import MarginRuleDaily
from app.models.order import Order
from app.models.position import Position
from app.repositories.daily_settlement_repository import DailySettlementRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.order_schema import OrderCancelRequest
from app.schemas.pnl_schema import AccountRealtimePnl, PositionRealtimePnl
from app.services.account_access_scope import AccountAccessScope
from app.services.account_risk_state_service import AccountRiskStateService
from app.services.account_valuation_calculator import AccountValuationCalculator
from app.services.active_order_rebuild_service import ActiveOrderRebuildService
from app.services.option_margin_adjustment_service import OptionMarginAdjustmentService
from app.services.option_margin_calculator import OptionMarginInput
from app.services.option_margin_calculator_resolver import OptionMarginCalculatorResolver
from app.services.order_cancellation_service import OrderCancellationService
from app.services.realtime_fact_event_service import RealtimeFactEventService
from app.services.settlement_gate_service import DAILY_SETTLEMENT_ADVISORY_LOCK_KEY
from app.services.settlement_replay_service import (
    OPTION_TYPES,
    ReplayedPosition,
    SettlementReplayService,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
RISK_QUANT = Decimal("0.00000001")
ZERO = Decimal("0.000000")


@dataclass(frozen=True)
class SettlementInstrument:
    id: int
    order_book_id: str
    exchange_id: str
    symbol: str
    product_id: str
    instrument_type: str
    multiplier: Decimal
    expire_date: date | None
    last_trading_date: date | None
    underlying_instrument_id: int | None
    option_type: str | None
    strike_price: Decimal | None
    is_tradeable: bool = True

    @classmethod
    def from_model(cls, item: Instrument) -> "SettlementInstrument":
        return cls(
            id=item.id,
            order_book_id=item.order_book_id,
            exchange_id=item.exchange_id,
            symbol=item.symbol,
            product_id=item.product_id or item.symbol,
            instrument_type=item.instrument_type,
            multiplier=Decimal(item.contract_multiplier),
            expire_date=item.expire_date,
            last_trading_date=item.last_trading_date,
            underlying_instrument_id=item.underlying_instrument_id,
            option_type=item.option_type,
            strike_price=(Decimal(item.strike_price) if item.strike_price is not None else None),
            is_tradeable=bool(item.is_tradeable),
        )


@dataclass(frozen=True)
class FrozenPrice:
    exchange_id: str
    symbol: str
    price: Decimal
    tick_time: datetime
    tick_trading_day: date
    source_event_id: str


@dataclass(frozen=True)
class FuturesMarginRule:
    long_rate: Decimal
    short_rate: Decimal


@dataclass(frozen=True)
class DailySettlementResult:
    batch_id: str
    trading_day: date
    status: str
    current_stage: str
    accounts_settled: int
    already_completed: bool
    cache_status: str
    cache_message: str | None = None


class DailySettlementError(AppError):
    """带命令行恢复上下文的结算异常。"""

    error_code = "DAILY_SETTLEMENT_FAILED"

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        error_code: str | None = None,
        batch_id: str | None = None,
        account_id: str | None = None,
        retriable: bool = True,
    ) -> None:
        super().__init__(message, error_code=error_code)
        self.stage = stage
        self.batch_id = batch_id
        self.account_id = account_id
        self.retriable = retriable


def _decimal_text(value: Decimal | None) -> str:
    return format(quantize_money(value or ZERO), "f")


def _account_snapshot(account: Any) -> dict[str, Any]:
    names = (
        "cash_balance",
        "available_cash",
        "frozen_cash",
        "equity",
        "used_margin",
        "frozen_margin",
        "realized_pnl",
        "unrealized_pnl",
        "daily_position_pnl",
        "daily_close_pnl",
        "daily_commission",
        "daily_pnl",
        "cumulative_net_pnl",
        "used_commission",
        "frozen_commission",
        "option_used_margin",
        "option_realtime_required_margin",
        "long_option_market_value",
        "short_option_market_value",
        "net_option_market_value",
        "risk_available_cash",
        "risk_ratio",
    )
    values = {name: _decimal_text(Decimal(getattr(account, name, 0))) for name in names}
    values.update(
        {
            "account_id": account.account_id,
            "risk_state": account.risk_state,
            "status": account.status,
            "trading_day": account.trading_day.isoformat() if account.trading_day else None,
        }
    )
    return values


def _position_snapshot(position: Any) -> dict[str, Any]:
    return {
        "position_id": position.position_id,
        "total_volume": position.total_volume,
        "today_volume": position.today_volume,
        "yesterday_volume": position.yesterday_volume,
        "frozen_volume": position.frozen_volume,
        "available_volume": position.available_volume,
        "average_open_price": _decimal_text(Decimal(position.average_open_price)),
        "position_cost": _decimal_text(Decimal(position.position_cost)),
        "used_margin": _decimal_text(Decimal(position.used_margin)),
        "realtime_required_margin": _decimal_text(
            Decimal(position.realtime_required_margin)
        ),
        "option_market_value": _decimal_text(Decimal(position.option_market_value)),
        "realized_pnl": _decimal_text(Decimal(position.realized_pnl)),
        "unrealized_pnl": _decimal_text(Decimal(position.unrealized_pnl)),
        "daily_position_pnl": _decimal_text(Decimal(position.daily_position_pnl)),
        "daily_close_pnl": _decimal_text(Decimal(position.daily_close_pnl)),
        "trading_day": position.trading_day.isoformat(),
    }


class DailySettlementService:
    """一次性手工日终结算编排；PostgreSQL 是唯一资金事实来源。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] = SessionLocal,
        database_engine: Engine = engine,
        repository: DailySettlementRepository | None = None,
        order_repository: OrderRepository | None = None,
        cancellation_service: OrderCancellationService | None = None,
        tick_store: MarketTickStore | None = None,
        outbox_repository: OutboxRepository | None = None,
        option_margin_resolver: OptionMarginCalculatorResolver | None = None,
        time_provider: Callable[[], datetime] = utc_now,
        tick_max_age_seconds: int | None = None,
        redis_recovery_enabled: bool = True,
    ) -> None:
        self.session_factory = session_factory
        self.database_engine = database_engine
        self.repository = repository or DailySettlementRepository()
        self.order_repository = order_repository or OrderRepository()
        self.outbox_repository = outbox_repository or OutboxRepository()
        self.cancellation_service = cancellation_service or OrderCancellationService()
        self.tick_store = tick_store or MarketTickStore(
            redis_client, stream_name=settings.market_tick_stream_name
        )
        self.option_margin_resolver = (
            option_margin_resolver or OptionMarginCalculatorResolver()
        )
        self.time_provider = time_provider
        self.tick_max_age_seconds = tick_max_age_seconds or getattr(
            settings, "daily_settlement_tick_max_age_seconds", 3600
        )
        self.redis_recovery_enabled = redis_recovery_enabled
        self.replay_service = SettlementReplayService()

    def _now(self) -> datetime:
        value = self.time_provider()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def _acquire_exclusive_lock(self, connection: Connection) -> None:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_lock(:lock_key)"),
                {"lock_key": DAILY_SETTLEMENT_ADVISORY_LOCK_KEY},
            )
            connection.commit()

    def _release_exclusive_lock(self, connection: Connection) -> None:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": DAILY_SETTLEMENT_ADVISORY_LOCK_KEY},
            )
            connection.commit()

    @staticmethod
    def _instrument_models(
        db: Session,
        *,
        include_active_orders: bool,
        trading_day: date | None = None,
    ) -> list[Instrument]:
        codes = set(
            db.scalars(
                select(Position.order_book_id).where(Position.total_volume > 0)
            ).all()
        )
        if include_active_orders:
            codes.update(
                db.scalars(
                    select(Order.order_book_id).where(
                        Order.status.in_(("ACCEPTED", "PARTIALLY_FILLED")),
                        Order.remaining_volume > 0,
                    )
                ).all()
            )
        if trading_day is not None:
            codes.update(
                DailySettlementRepository.list_replay_order_book_ids(
                    db, trading_day
                )
            )
        if not codes:
            return []
        direct = db.scalars(
            select(Instrument).where(Instrument.order_book_id.in_(tuple(codes)))
        ).all()
        underlying_ids = {
            item.underlying_instrument_id
            for item in direct
            if item.underlying_instrument_id is not None
        }
        underlyings = (
            db.scalars(select(Instrument).where(Instrument.id.in_(underlying_ids))).all()
            if underlying_ids
            else []
        )
        result = {item.id: item for item in [*direct, *underlyings]}
        return [result[key] for key in sorted(result)]

    def _load_instruments(
        self,
        db: Session,
        *,
        include_active_orders: bool,
        trading_day: date | None = None,
    ) -> tuple[dict[str, SettlementInstrument], dict[int, SettlementInstrument]]:
        items = [
            SettlementInstrument.from_model(item)
            for item in self._instrument_models(
                db,
                include_active_orders=include_active_orders,
                trading_day=trading_day,
            )
        ]
        return (
            {item.order_book_id: item for item in items},
            {item.id: item for item in items},
        )

    def _preflight(self, trading_day: date) -> date:
        now = self._now().astimezone(SHANGHAI)
        if trading_day > now.date():
            raise DailySettlementError(
                "禁止结算未来交易日",
                stage=DailySettlementStage.PREFLIGHT.value,
                error_code="FUTURE_TRADING_DAY",
                retriable=False,
            )
        with self.session_factory() as db:
            calendars = self.repository.list_open_calendars(db, trading_day)
            instruments, _ = self._load_instruments(
                db,
                include_active_orders=True,
                trading_day=trading_day,
            )
            relevant = [
                item for item in instruments.values() if item.is_tradeable
            ]
            relevant_exchanges = {item.exchange_id for item in relevant}
            selected_calendars = [
                row
                for row in calendars
                if not relevant_exchanges
                or row["exchange_id"] in relevant_exchanges
            ]
            found_exchanges = {
                row["exchange_id"] for row in selected_calendars
            }
            missing_exchanges = relevant_exchanges - found_exchanges
            if missing_exchanges:
                raise DailySettlementError(
                    f"相关交易所缺少交易日历: {sorted(missing_exchanges)!r}",
                    stage=DailySettlementStage.PREFLIGHT.value,
                    error_code="TRADING_CALENDAR_MISSING",
                )
            if not selected_calendars or not all(
                row["is_open"]
                and str(row["status"]).upper() == "OPEN"
                for row in selected_calendars
            ):
                raise DailySettlementError(
                    "目标日期不是相关交易所的完整有效交易日",
                    stage=DailySettlementStage.PREFLIGHT.value,
                    error_code="INVALID_TRADING_DAY",
                    retriable=False,
                )
            # 标的指数会随股指期权一起加载，用于冻结结算价格和计算期权
            # 保证金，但它本身不是本系统可交易的期货产品。参考数据表的
            # instrument_type约束也只覆盖期货、商品期权和股指期权，因此
            # 这里只校验实际可交易合约的产品时段。
            product_keys = {
                (item.exchange_id, item.product_id, item.instrument_type)
                for item in relevant
                if item.instrument_type != InstrumentType.INDEX.value
            }
            schedules = self.repository.list_product_schedules(
                db, trading_day, product_keys
            )
            found = {
                (row["exchange_id"], row["product_code"], row["instrument_type"])
                for row in schedules
            }
            missing = product_keys - found
            if missing:
                raise DailySettlementError(
                    f"相关产品缺少交易时段: {sorted(missing)!r}",
                    stage=DailySettlementStage.PREFLIGHT.value,
                    error_code="TRADING_SCHEDULE_MISSING",
                    retriable=True,
                )
            for row in schedules:
                sessions = row["sessions"]
                if isinstance(sessions, str):
                    sessions = json.loads(sessions)
                if not sessions:
                    raise DailySettlementError(
                        f"产品交易时段为空: {row['exchange_id']} {row['product_code']}",
                        stage=DailySettlementStage.PREFLIGHT.value,
                        error_code="TRADING_SCHEDULE_EMPTY",
                    )
                try:
                    latest_end = max(
                        datetime.fromisoformat(str(item["end_at"]))
                        for item in sessions
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise DailySettlementError(
                        "产品交易时段格式不合法",
                        stage=DailySettlementStage.PREFLIGHT.value,
                        error_code="TRADING_SCHEDULE_INVALID",
                        retriable=False,
                    ) from exc
                if latest_end.tzinfo is None:
                    latest_end = latest_end.replace(tzinfo=SHANGHAI)
                if now < latest_end.astimezone(SHANGHAI):
                    raise DailySettlementError(
                        f"相关产品尚未收盘: {row['exchange_id']} {row['product_code']}",
                        stage=DailySettlementStage.PREFLIGHT.value,
                        error_code="PRODUCT_NOT_CLOSED",
                    )
            next_days = {
                row["next_trading_day"]
                for row in selected_calendars
            }
            if len(next_days) != 1 or None in next_days:
                raise DailySettlementError(
                    "相关交易所下一交易日不一致",
                    stage=DailySettlementStage.PREFLIGHT.value,
                    error_code="NEXT_TRADING_DAY_INCONSISTENT",
                    retriable=False,
                )
            earlier = self.repository.get_earlier_incomplete_batch(db, trading_day)
            if earlier is not None:
                raise DailySettlementError(
                    f"更早交易日 {earlier.trading_day.isoformat()} 尚未完成结算",
                    stage=DailySettlementStage.PREFLIGHT.value,
                    error_code="EARLIER_SETTLEMENT_INCOMPLETE",
                    batch_id=earlier.batch_id,
                )
            return next(iter(next_days))

    def _load_or_start_batch(self, trading_day: date) -> DailySettlementBatch:
        now = self._now()
        with self.session_factory() as db:
            batch = self.repository.get_batch(db, trading_day, for_update=True)
            if batch is None:
                batch = DailySettlementBatch(
                    batch_id=f"DS-{trading_day:%Y%m%d}-{uuid4().hex[:12].upper()}",
                    trading_day=trading_day,
                    status=DailySettlementBatchStatus.RUNNING.value,
                    current_stage=DailySettlementStage.PREFLIGHT.value,
                    started_at=now,
                    cache_status=SettlementCacheStatus.PENDING.value,
                    created_at=now,
                    updated_at=now,
                )
                self.repository.add(db, batch)
            elif batch.status != DailySettlementBatchStatus.COMPLETED.value:
                batch.status = DailySettlementBatchStatus.RUNNING.value
                batch.failed_at = None
                batch.failure_code = None
                batch.failure_message = None
                batch.failure_account_id = None
                batch.updated_at = now
            db.commit()
            db.refresh(batch)
            db.expunge(batch)
            return batch

    def _advance(self, trading_day: date, stage: DailySettlementStage) -> None:
        with self.session_factory() as db:
            batch = self.repository.get_batch(db, trading_day, for_update=True)
            if batch is None:
                raise DataAccessError("日终结算批次不存在")
            batch.current_stage = stage.value
            batch.updated_at = self._now()
            db.commit()

    def _cancel_active_orders(self) -> int:
        cancelled = 0
        while True:
            with self.session_factory() as db:
                rows = self.order_repository.list_active_after_id(
                    db, last_id=0, batch_size=1
                )
                target = (
                    (rows[0].order_id, rows[0].account_id) if rows else None
                )
            if target is None:
                return cancelled
            order_id, account_id = target
            with self.session_factory() as db:
                self.cancellation_service.cancel_order(
                    db=db,
                    order_id=order_id,
                    request=OrderCancelRequest(account_id=account_id),
                    access_scope=AccountAccessScope.admin(),
                    settlement_internal=True,
                )
            cancelled += 1

    def _confirm_barrier(self) -> None:
        with self.session_factory() as db:
            active = self.repository.count_active_orders(db)
            if active:
                raise DailySettlementError(
                    f"成交持久化屏障后仍有 {active} 笔活动订单",
                    stage=DailySettlementStage.BARRIER_CONFIRMED.value,
                    error_code="ACTIVE_ORDER_BARRIER_FAILED",
                )

    def _validate_tick(
        self,
        *,
        values: dict[str, str],
        instrument: SettlementInstrument,
        trading_day: date,
    ) -> FrozenPrice:
        if not values:
            raise DailySettlementError(
                f"合约缺少 Tick: {instrument.exchange_id} {instrument.symbol}",
                stage=DailySettlementStage.PRICES_FROZEN.value,
                error_code="SETTLEMENT_TICK_MISSING",
            )
        try:
            tick = MarketTickStore.mapping_to_tick(values)
            price = Decimal(tick.last_price) if tick.last_price is not None else None
        except (ValueError, TypeError, InvalidOperation) as exc:
            raise DailySettlementError(
                f"合约 Tick 无法解析: {instrument.exchange_id} {instrument.symbol}",
                stage=DailySettlementStage.PRICES_FROZEN.value,
                error_code="SETTLEMENT_TICK_INVALID",
            ) from exc
        if (
            tick.exchange_id != instrument.exchange_id
            or tick.symbol != instrument.symbol
            or tick.order_book_id != instrument.order_book_id
        ):
            raise DailySettlementError(
                f"合约 Tick 标识不匹配: {instrument.order_book_id}",
                stage=DailySettlementStage.PRICES_FROZEN.value,
                error_code="SETTLEMENT_TICK_CONTRACT_MISMATCH",
            )
        if price is None or not price.is_finite() or price <= 0:
            raise DailySettlementError(
                f"合约 last_price 不是有限正数: {instrument.order_book_id}",
                stage=DailySettlementStage.PRICES_FROZEN.value,
                error_code="SETTLEMENT_PRICE_INVALID",
            )
        if tick.trading_day != trading_day:
            raise DailySettlementError(
                f"Tick 交易日不匹配: {instrument.order_book_id}",
                stage=DailySettlementStage.PRICES_FROZEN.value,
                error_code="SETTLEMENT_TICK_TRADING_DAY_MISMATCH",
            )
        tick_time = tick.event_time
        if tick_time.tzinfo is None:
            tick_time = tick_time.replace(tzinfo=SHANGHAI)
        age = self._now().astimezone(timezone.utc) - tick_time.astimezone(timezone.utc)
        if age < timedelta(seconds=-60) or age > timedelta(
            seconds=self.tick_max_age_seconds
        ):
            raise DailySettlementError(
                f"Tick 时间过旧或来自未来: {instrument.order_book_id}",
                stage=DailySettlementStage.PRICES_FROZEN.value,
                error_code="SETTLEMENT_TICK_STALE",
            )
        return FrozenPrice(
            exchange_id=instrument.exchange_id,
            symbol=instrument.symbol,
            price=quantize_money(price),
            tick_time=tick_time,
            tick_trading_day=tick.trading_day,
            source_event_id=tick.source_event_id,
        )

    def _freeze_prices(
        self,
        *,
        batch: DailySettlementBatch,
        trading_day: date,
        instruments: dict[str, SettlementInstrument],
        instruments_by_id: dict[int, SettlementInstrument],
    ) -> dict[tuple[str, str], FrozenPrice]:
        required = {
            (item.exchange_id, item.symbol): item
            for item in instruments.values()
            if item.order_book_id in instruments
        }
        # instruments 已包含所有活动持仓及其期权标的；无持仓时集合为空。
        with self.session_factory() as db:
            existing = self.repository.list_frozen_prices(db, trading_day)
            if existing:
                result = {
                    (item.exchange_id, item.symbol): FrozenPrice(
                        item.exchange_id,
                        item.symbol,
                        Decimal(item.settlement_price),
                        item.source_tick_time,
                        item.source_tick_trading_day,
                        item.source_event_id,
                    )
                    for item in existing
                }
                # 已完成账户中的到期期权会在首轮结算时关闭，恢复执行时不再
                # 出现在活动持仓集合中；已有冻结价因此可以是当前依赖的超集。
                if not set(required).issubset(result):
                    raise DailySettlementError(
                        "已冻结结算价集合与当前持仓依赖不一致",
                        stage=DailySettlementStage.PRICES_FROZEN.value,
                        error_code="FROZEN_PRICE_SET_INCONSISTENT",
                        batch_id=batch.batch_id,
                        retriable=False,
                    )
                return result

        frozen: dict[tuple[str, str], FrozenPrice] = {}
        # 公共业务接口逐唯一合约恰好调用一次，禁止直接拼 Redis Key。
        for key, instrument in sorted(required.items()):
            frozen[key] = self._validate_tick(
                values=self.tick_store.get_latest(*key),
                instrument=instrument,
                trading_day=trading_day,
            )
        now = self._now()
        with self.session_factory() as db:
            for key, item in frozen.items():
                instrument = required[key]
                self.repository.add(
                    db,
                    InstrumentSettlementPrice(
                        batch_id=batch.batch_id,
                        trading_day=trading_day,
                        exchange_id=instrument.exchange_id,
                        symbol=instrument.symbol,
                        order_book_id=instrument.order_book_id,
                        instrument_type=instrument.instrument_type,
                        settlement_price=item.price,
                        price_source="YMM_LIVE_DATA_LAST_PRICE",
                        source_tick_time=item.tick_time,
                        source_tick_trading_day=item.tick_trading_day,
                        source_event_id=item.source_event_id,
                        created_at=now,
                    ),
                )
            db.commit()
        return frozen

    def _load_futures_rules(
        self, db: Session, trading_day: date
    ) -> dict[tuple[str, str], FuturesMarginRule]:
        rows = db.scalars(
            select(MarginRuleDaily).where(MarginRuleDaily.trading_day == trading_day)
        ).all()
        return {
            (item.exchange_id, item.symbol): FuturesMarginRule(
                Decimal(item.long_margin_rate), Decimal(item.short_margin_rate)
            )
            for item in rows
        }

    @staticmethod
    def _allocate_margin(details: Sequence[Any], total: Decimal) -> None:
        active = [item for item in details if item.remaining_volume > 0]
        total_volume = sum(item.remaining_volume for item in active)
        if not active or total_volume <= 0:
            if total != ZERO:
                raise DataAccessError("无有效明细却存在结算保证金")
            return
        remaining = total
        for index, detail in enumerate(active):
            share = (
                remaining
                if index == len(active) - 1
                else quantize_money(
                    total * Decimal(detail.remaining_volume) / Decimal(total_volume)
                )
            )
            detail.remaining_margin = share
            detail.realtime_required_margin = share
            remaining = quantize_money(remaining - share)

    def _option_margin(
        self,
        *,
        position: Any,
        instrument: SettlementInstrument,
        underlying: SettlementInstrument,
        option_price: Decimal,
        underlying_price: Decimal,
        now: datetime,
    ) -> Decimal:
        rule = OptionMarginAdjustmentService._rule(position)
        underlying_multiplier = Decimal("1")
        underlying_margin_per_lot = ZERO
        if instrument.instrument_type == InstrumentType.FUTURES_OPTION.value:
            underlying_rate, underlying_multiplier = (
                OptionMarginAdjustmentService._commodity_underlying_inputs(position)
            )
            underlying_margin_per_lot = quantize_money(
                underlying_price * underlying_multiplier * underlying_rate
            )
        calculator = self.option_margin_resolver.resolve(
            instrument_type=instrument.instrument_type,
            exchange_id=instrument.exchange_id,
            margin_algorithm=rule.margin_algorithm,
        )
        if instrument.option_type is None or instrument.strike_price is None:
            raise DataAccessError("期权类型或执行价缺失")
        return calculator.calculate(
            OptionMarginInput(
                option_type=OptionType(instrument.option_type),
                strike_price=instrument.strike_price,
                option_price=option_price,
                underlying_price=underlying_price,
                option_multiplier=Decimal(position.multiplier_snapshot),
                underlying_multiplier=underlying_multiplier,
                volume=position.total_volume,
                price_mode=MarginPriceMode.SETTLEMENT,
                calculated_at=now,
                rule=rule,
                underlying_margin_per_lot=underlying_margin_per_lot,
            )
        ).total_margin

    @staticmethod
    def _trade_cash_effect(trade: Any) -> Decimal:
        commission = Decimal(trade.commission)
        if trade.instrument_type == InstrumentType.FUTURES.value:
            return quantize_money(Decimal(trade.daily_close_pnl) - commission)
        if trade.instrument_type in OPTION_TYPES:
            return quantize_money(
                Decimal(trade.premium_cash_flow) - commission
            )
        raise DataAccessError(
            "成交合约类型不支持现金回放",
            error_code="REPLAY_TRADE_TYPE_UNSUPPORTED",
        )

    def _settle_batch_from_facts(
        self,
        *,
        batch: DailySettlementBatch,
        trading_day: date,
        next_trading_day: date,
        instruments: dict[str, SettlementInstrument],
        instruments_by_id: dict[int, SettlementInstrument],
        prices: dict[tuple[str, str], FrozenPrice],
    ) -> int:
        """在一个事务中回放不可变事实、守恒对账并切换全部账户交易日。"""

        with self.session_factory() as db:
            try:
                accounts = list(self.repository.lock_all_accounts(db))
                positions = list(self.repository.lock_all_positions(db))
                details = list(
                    self.repository.lock_position_details_through_day(
                        db, trading_day
                    )
                )
                trades = list(
                    self.repository.list_trades_through_day(db, trading_day)
                )
                allocations = list(
                    self.repository.list_trade_allocations_through_day(
                        db, trading_day
                    )
                )
                prior_position_rows = list(
                    self.repository.list_prior_position_settlements(
                        db, trading_day
                    )
                )
                prior_expiry_rows = list(
                    self.repository.list_prior_expiry_settlements(
                        db, trading_day
                    )
                )
                prior_account_rows = list(
                    self.repository.list_prior_account_settlements(
                        db, trading_day
                    )
                )
                existing_results = db.scalars(
                    select(DailyAccountSettlement).where(
                        DailyAccountSettlement.trading_day == trading_day
                    )
                ).all()
                if existing_results:
                    raise DataAccessError(
                        "原子回放批次存在遗留账户结果，禁止混用旧的逐账户提交模式",
                        error_code="ATOMIC_REPLAY_RESULT_ALREADY_EXISTS",
                    )

                latest_prior_positions: dict[str, DailyPositionSettlement] = {}
                for item in prior_position_rows:
                    latest_prior_positions.setdefault(item.position_id, item)
                prior_expired_ids = {
                    item.position_id for item in prior_expiry_rows
                }
                stale_expired = [
                    item.position_id
                    for item in positions
                    if item.position_id in prior_expired_ids
                    and int(item.total_volume) != 0
                ]
                if stale_expired:
                    raise DataAccessError(
                        "历史已到期持仓仍有活动数量: "
                        + ", ".join(sorted(stale_expired)),
                        error_code="HISTORICAL_EXPIRED_POSITION_ACTIVE",
                    )
                price_values = {
                    key: item.price for key, item in prices.items()
                }
                replay = self.replay_service.replay(
                    trading_day=trading_day,
                    details=details,
                    trades=trades,
                    allocations=allocations,
                    prior_position_settlements=latest_prior_positions,
                    prior_expired_position_ids=prior_expired_ids,
                    instruments=instruments,
                    instruments_by_id=instruments_by_id,
                    prices=price_values,
                    has_prior_batch=bool(prior_account_rows),
                )

                account_by_id = {item.account_id: item for item in accounts}
                position_by_id = {item.position_id: item for item in positions}
                detail_by_id = {
                    item.position_detail_id: item for item in details
                }
                projections_by_account: dict[str, list[ReplayedPosition]] = {}
                for item in replay.positions:
                    projections_by_account.setdefault(
                        item.account_id, []
                    ).append(item)
                    if item.position_id not in position_by_id:
                        raise DataAccessError(
                            "不可变开仓明细对应的汇总持仓不存在",
                            error_code="REPLAY_POSITION_AGGREGATE_MISSING",
                        )
                    if item.account_id not in account_by_id:
                        raise DataAccessError(
                            "不可变成交对应账户不存在",
                            error_code="REPLAY_ACCOUNT_MISSING",
                        )

                futures_rules = self._load_futures_rules(db, trading_day)
                prior_futures_cash: dict[str, Decimal] = {}
                for item in prior_account_rows:
                    prior_futures_cash[item.account_id] = quantize_money(
                        prior_futures_cash.get(item.account_id, ZERO)
                        + Decimal(item.futures_settlement_pnl)
                    )
                prior_expiry_cash: dict[str, Decimal] = {}
                prior_expiry_realized: dict[str, Decimal] = {}
                for item in prior_expiry_rows:
                    prior_expiry_cash[item.account_id] = quantize_money(
                        prior_expiry_cash.get(item.account_id, ZERO)
                        + Decimal(item.cash_flow)
                    )
                    prior_expiry_realized[item.account_id] = quantize_money(
                        prior_expiry_realized.get(item.account_id, ZERO)
                        + Decimal(getattr(item, "realized_pnl", ZERO))
                    )
                all_trades_by_account: dict[str, list[Any]] = {}
                for trade in trades:
                    all_trades_by_account.setdefault(
                        trade.account_id, []
                    ).append(trade)

                now = self._now()
                events = RealtimeFactEventService(
                    repository=self.outbox_repository
                )
                affected_positions: list[Any] = []
                position_results: list[DailyPositionSettlement] = []
                account_results: list[DailyAccountSettlement] = []
                for account in accounts:
                    if any(
                        Decimal(getattr(account, name)) != ZERO
                        for name in (
                            "frozen_margin",
                            "frozen_cash",
                            "frozen_commission",
                        )
                    ):
                        raise DataAccessError(
                            "撤单后账户仍有无法解释的冻结资金",
                            error_code=(
                                "SETTLEMENT_FROZEN_RESOURCE_INCONSISTENT"
                            ),
                        )
                    account_id = account.account_id
                    account_positions = projections_by_account.get(
                        account_id, []
                    )
                    account_trades = all_trades_by_account.get(
                        account_id, []
                    )
                    opening_trade_cash = quantize_money(
                        sum(
                            (
                                self._trade_cash_effect(item)
                                for item in account_trades
                                if item.trading_day < trading_day
                            ),
                            ZERO,
                        )
                    )
                    today_trade_cash = quantize_money(
                        sum(
                            (
                                self._trade_cash_effect(item)
                                for item in account_trades
                                if item.trading_day == trading_day
                            ),
                            ZERO,
                        )
                    )
                    opening_cash = quantize_money(
                        Decimal(account.initial_cash)
                        + opening_trade_cash
                        + prior_futures_cash.get(account_id, ZERO)
                        + prior_expiry_cash.get(account_id, ZERO)
                    )
                    expected_before_cash = quantize_money(
                        opening_cash + today_trade_cash
                    )
                    actual_before_cash = quantize_money(
                        Decimal(account.cash_balance)
                    )
                    if actual_before_cash != expected_before_cash:
                        raise DataAccessError(
                            "账户现金与不可变成交及历史结算事实不守恒: "
                            f"account={account_id} actual={actual_before_cash} "
                            f"expected={expected_before_cash}",
                            error_code="REPLAY_OPENING_CASH_MISMATCH",
                        )

                    before_account = _account_snapshot(account)
                    total_used_margin = ZERO
                    option_used_margin = ZERO
                    long_option_value = ZERO
                    short_option_value = ZERO
                    futures_holding_pnl = ZERO
                    futures_close_pnl = ZERO
                    option_economic_pnl = ZERO
                    option_premium_cash = ZERO
                    expiry_cash = ZERO
                    day_commission = ZERO
                    active_cumulative_economic = ZERO

                    for projection in account_positions:
                        position = position_by_id[projection.position_id]
                        instrument = instruments[projection.order_book_id]
                        before_position = _position_snapshot(position)
                        replay_details = [
                            detail_by_id[item.position_detail_id]
                            for item in projection.details
                        ]
                        replay_detail_map = {
                            item.position_detail_id: item
                            for item in projection.details
                        }
                        for detail in replay_details:
                            detail_result = replay_detail_map[
                                detail.position_detail_id
                            ]
                            detail.remaining_volume = (
                                detail_result.ending_volume
                            )
                            detail.frozen_volume = 0
                            detail.status = (
                                PositionDetailStatus.OPEN.value
                                if detail.remaining_volume > 0
                                else PositionDetailStatus.CLOSED.value
                            )
                            if detail.remaining_volume > 0:
                                detail.pnl_base_price = (
                                    projection.settlement_price
                                )
                            detail.updated_at = now

                        position.total_volume = projection.ending_volume
                        position.today_volume = 0
                        position.yesterday_volume = projection.ending_volume
                        position.frozen_volume = 0
                        position.available_volume = projection.ending_volume
                        remaining_cost = quantize_money(
                            sum(
                                Decimal(item.open_price)
                                * projection.multiplier
                                * Decimal(item.remaining_volume)
                                for item in replay_details
                            )
                        )
                        position.position_cost = remaining_cost
                        position.average_open_price = (
                            quantize_money(
                                remaining_cost
                                / projection.multiplier
                                / Decimal(projection.ending_volume)
                            )
                            if projection.ending_volume > 0
                            else ZERO
                        )
                        position.realized_pnl = (
                            projection.cumulative_realized_pnl
                        )
                        position.unrealized_pnl = (
                            projection.cumulative_economic_pnl
                        )
                        position.daily_position_pnl = ZERO
                        position.daily_close_pnl = ZERO
                        position.trading_day = next_trading_day
                        position.updated_at = now

                        settlement_margin = ZERO
                        option_market_value = ZERO
                        if (
                            projection.instrument_type
                            == InstrumentType.FUTURES.value
                            and projection.ending_volume > 0
                        ):
                            rule = futures_rules.get(
                                (
                                    projection.exchange_id,
                                    projection.symbol,
                                )
                            )
                            if rule is None:
                                raise DataAccessError(
                                    "期货日保证金规则不存在",
                                    error_code="SETTLEMENT_MARGIN_RULE_MISSING",
                                )
                            rate = (
                                rule.long_rate
                                if projection.direction
                                == PositionDirection.LONG.value
                                else rule.short_rate
                            )
                            settlement_margin = quantize_money(
                                projection.settlement_price
                                * projection.multiplier
                                * Decimal(projection.ending_volume)
                                * rate
                            )
                            self._allocate_margin(
                                replay_details, settlement_margin
                            )
                            for detail in replay_details:
                                detail.realtime_required_margin = ZERO
                            futures_holding_pnl = quantize_money(
                                futures_holding_pnl
                                + projection.holding_pnl
                            )
                            futures_close_pnl = quantize_money(
                                futures_close_pnl + projection.close_pnl
                            )
                        elif (
                            projection.instrument_type in OPTION_TYPES
                            and projection.ending_volume > 0
                        ):
                            underlying = instruments_by_id.get(
                                instrument.underlying_instrument_id or -1
                            )
                            if underlying is None:
                                raise DataAccessError(
                                    "期权标的合约不存在"
                                )
                            underlying_price = prices[
                                (
                                    underlying.exchange_id,
                                    underlying.symbol,
                                )
                            ].price
                            option_market_value = quantize_money(
                                projection.settlement_price
                                * projection.multiplier
                                * Decimal(projection.ending_volume)
                            )
                            if (
                                projection.direction
                                == PositionDirection.SHORT.value
                            ):
                                settlement_margin = self._option_margin(
                                    position=position,
                                    instrument=instrument,
                                    underlying=underlying,
                                    option_price=(
                                        projection.settlement_price
                                    ),
                                    underlying_price=underlying_price,
                                    now=now,
                                )
                                short_option_value = quantize_money(
                                    short_option_value
                                    + option_market_value
                                )
                                option_used_margin = quantize_money(
                                    option_used_margin + settlement_margin
                                )
                            else:
                                long_option_value = quantize_money(
                                    long_option_value
                                    + option_market_value
                                )
                            self._allocate_margin(
                                replay_details, settlement_margin
                            )
                            position.margin_price_mode = (
                                MarginPriceMode.SETTLEMENT.value
                            )
                            position.margin_option_price = (
                                projection.settlement_price
                            )
                            position.margin_underlying_price = (
                                underlying_price
                            )
                            position.margin_calculated_at = now
                            for detail in replay_details:
                                detail.margin_price_mode = (
                                    MarginPriceMode.SETTLEMENT.value
                                )
                                detail.margin_option_price = (
                                    projection.settlement_price
                                )
                                detail.margin_underlying_price = (
                                    underlying_price
                                )
                                detail.margin_calculated_at = now
                        else:
                            for detail in replay_details:
                                detail.remaining_margin = ZERO
                                detail.realtime_required_margin = ZERO

                        position.used_margin = settlement_margin
                        position.realtime_required_margin = (
                            settlement_margin
                            if projection.instrument_type in OPTION_TYPES
                            and projection.direction
                            == PositionDirection.SHORT.value
                            else ZERO
                        )
                        position.option_market_value = option_market_value
                        total_used_margin = quantize_money(
                            total_used_margin + settlement_margin
                        )
                        if projection.instrument_type in OPTION_TYPES:
                            option_economic_pnl = quantize_money(
                                option_economic_pnl
                                + projection.option_economic_pnl
                            )
                            option_premium_cash = quantize_money(
                                option_premium_cash
                                + projection.premium_cash_flow
                            )
                        expiry_cash = quantize_money(
                            expiry_cash + projection.expiry_cash_flow
                        )
                        day_commission = quantize_money(
                            day_commission + projection.commission
                        )
                        active_cumulative_economic = quantize_money(
                            active_cumulative_economic
                            + projection.cumulative_economic_pnl
                        )

                        if projection.expired_closed:
                            underlying = instruments_by_id[
                                instrument.underlying_instrument_id
                            ]
                            self.repository.add(
                                db,
                                OptionExpirySettlementDetail(
                                    batch_id=batch.batch_id,
                                    trading_day=trading_day,
                                    account_id=account_id,
                                    position_id=projection.position_id,
                                    option_order_book_id=(
                                        projection.order_book_id
                                    ),
                                    option_type=instrument.option_type,
                                    direction=projection.direction,
                                    underlying_order_book_id=(
                                        underlying.order_book_id
                                    ),
                                    underlying_exchange_id=(
                                        underlying.exchange_id
                                    ),
                                    underlying_symbol=underlying.symbol,
                                    underlying_settlement_price=prices[
                                        (
                                            underlying.exchange_id,
                                            underlying.symbol,
                                        )
                                    ].price,
                                    strike_price=Decimal(
                                        instrument.strike_price
                                    ),
                                    multiplier_snapshot=(
                                        projection.multiplier
                                    ),
                                    quantity=(
                                        projection.ending_volume_before_expiry
                                    ),
                                    intrinsic_value=(
                                        projection.expiry_intrinsic_value
                                    ),
                                    gross_cash_amount=abs(
                                        projection.expiry_cash_flow
                                    ),
                                    cash_flow=projection.expiry_cash_flow,
                                    realized_pnl=(
                                        projection.expiry_realized_pnl
                                    ),
                                    settled_at=now,
                                ),
                            )

                        after_position = _position_snapshot(position)
                        before_position.update(
                            {
                                "authoritative_opening_yesterday_volume": (
                                    projection.opening_yesterday_volume
                                ),
                                "authoritative_today_open_volume": (
                                    projection.today_open_volume
                                ),
                                "authoritative_today_close_volume": (
                                    projection.today_close_volume
                                ),
                            }
                        )
                        position_result = DailyPositionSettlement(
                                batch_id=batch.batch_id,
                                trading_day=trading_day,
                                account_id=account_id,
                                position_id=projection.position_id,
                                exchange_id=projection.exchange_id,
                                symbol=projection.symbol,
                                order_book_id=projection.order_book_id,
                                instrument_type=(
                                    projection.instrument_type
                                ),
                                direction=projection.direction,
                                multiplier_snapshot=projection.multiplier,
                                volume_before=(
                                    projection.ending_volume_before_expiry
                                ),
                                opening_yesterday_volume=(
                                    projection.opening_yesterday_volume
                                ),
                                today_open_volume=(
                                    projection.today_open_volume
                                ),
                                today_close_volume=(
                                    projection.today_close_volume
                                ),
                                today_close_today_volume=(
                                    projection.today_close_today_volume
                                ),
                                today_close_yesterday_volume=(
                                    projection.today_close_yesterday_volume
                                ),
                                today_volume_before=max(
                                    projection.today_open_volume
                                    - projection.today_close_today_volume,
                                    0,
                                ),
                                yesterday_volume_before=max(
                                    projection.opening_yesterday_volume
                                    - projection.today_close_yesterday_volume,
                                    0,
                                ),
                                volume_after=projection.ending_volume,
                                today_volume_after=0,
                                yesterday_volume_after=(
                                    projection.ending_volume
                                ),
                                previous_settlement_basis=(
                                    projection.previous_basis
                                ),
                                settlement_price=(
                                    projection.settlement_price
                                ),
                                daily_settlement_pnl=(
                                    projection.holding_pnl
                                ),
                                close_pnl=projection.close_pnl,
                                option_economic_pnl=(
                                    projection.option_economic_pnl
                                ),
                                commission=projection.commission,
                                premium_cash_flow=(
                                    projection.premium_cash_flow
                                ),
                                cumulative_economic_pnl=(
                                    projection.cumulative_economic_pnl
                                ),
                                settlement_margin=settlement_margin,
                                option_market_value=option_market_value,
                                expired_closed=(
                                    projection.expired_closed
                                ),
                                before_snapshot=before_position,
                                after_snapshot=after_position,
                                settled_at=now,
                            )
                        self.repository.add(
                            db,
                            position_result,
                        )
                        position_results.append(position_result)
                        affected_positions.append(position)

                    trade_commission = quantize_money(
                        sum(
                            (
                                Decimal(item.commission)
                                for item in account_trades
                                if item.trading_day == trading_day
                            ),
                            ZERO,
                        )
                    )
                    if trade_commission != day_commission:
                        raise DataAccessError(
                            "账户成交手续费与持仓回放汇总不守恒",
                            error_code="REPLAY_COMMISSION_CONSERVATION_FAILED",
                        )
                    ending_cash = quantize_money(
                        expected_before_cash
                        + futures_holding_pnl
                        + expiry_cash
                    )
                    account.cash_balance = ending_cash
                    account.used_margin = total_used_margin
                    account.option_used_margin = option_used_margin
                    account.option_realtime_required_margin = (
                        option_used_margin
                    )
                    account.unrealized_pnl = ZERO
                    account.long_option_market_value = long_option_value
                    account.short_option_market_value = short_option_value
                    account.net_option_market_value = quantize_money(
                        long_option_value - short_option_value
                    )
                    account.used_commission = quantize_money(
                        sum(
                            (Decimal(item.commission) for item in account_trades),
                            ZERO,
                        )
                    )
                    trade_realized = quantize_money(
                        sum(
                            (Decimal(item.realized_pnl) for item in account_trades),
                            ZERO,
                        )
                    )
                    expiry_realized = quantize_money(
                        sum(
                            (
                                item.expiry_realized_pnl
                                for item in account_positions
                            ),
                            ZERO,
                        )
                    )
                    account.realized_pnl = quantize_money(
                        trade_realized
                        + prior_expiry_realized.get(account_id, ZERO)
                        + expiry_realized
                    )
                    account.cumulative_net_pnl = quantize_money(
                        Decimal(account.realized_pnl)
                        + active_cumulative_economic
                        - Decimal(account.used_commission)
                    )
                    account.daily_position_pnl = quantize_money(
                        futures_holding_pnl
                        + option_economic_pnl
                        - sum(
                            (
                                item.close_pnl
                                for item in account_positions
                                if item.instrument_type in OPTION_TYPES
                            ),
                            ZERO,
                        )
                    )
                    total_close_pnl = quantize_money(
                        sum(
                            (item.close_pnl for item in account_positions),
                            ZERO,
                        )
                    )
                    account.daily_close_pnl = total_close_pnl
                    account.daily_commission = day_commission
                    account.daily_pnl = quantize_money(
                        futures_holding_pnl
                        + futures_close_pnl
                        + option_economic_pnl
                        - day_commission
                    )
                    expected_position_pnl = quantize_money(
                        sum(
                            (
                                item.holding_pnl
                                for item in account_positions
                            ),
                            ZERO,
                        )
                    )
                    expected_net_pnl = quantize_money(
                        expected_position_pnl
                        + total_close_pnl
                        - day_commission
                    )
                    if account.daily_position_pnl != expected_position_pnl:
                        raise DataAccessError(
                            "账户持仓盈亏与逐持仓事实汇总不守恒",
                            error_code=(
                                "REPLAY_POSITION_PNL_CONSERVATION_FAILED"
                            ),
                        )
                    if account.daily_pnl != expected_net_pnl:
                        raise DataAccessError(
                            "账户净盈亏与持仓、平仓及手续费不守恒",
                            error_code="REPLAY_NET_PNL_CONSERVATION_FAILED",
                        )
                    valuation = AccountValuationCalculator.calculate(
                        cash_balance=ending_cash,
                        futures_unrealized_pnl=ZERO,
                        long_option_market_value=long_option_value,
                        short_option_market_value=short_option_value,
                        used_margin=total_used_margin,
                        option_used_margin=option_used_margin,
                        option_realtime_required_margin=option_used_margin,
                        frozen_margin=ZERO,
                        frozen_cash=ZERO,
                        frozen_commission=ZERO,
                        option_collateral_ratio=(
                            settings.option_collateral_ratio
                        ),
                    )
                    account.equity = valuation.equity
                    account.available_cash = valuation.available_cash
                    account.risk_available_cash = (
                        valuation.risk_available_cash
                    )
                    account.net_option_market_value = (
                        valuation.net_option_market_value
                    )
                    account.risk_ratio = (
                        ZERO.quantize(RISK_QUANT)
                        if account.equity <= ZERO
                        else (
                            valuation.effective_required_margin
                            / account.equity
                        ).quantize(RISK_QUANT, rounding=ROUND_HALF_UP)
                    )
                    account.risk_state = AccountRiskStateService.evaluate(
                        current_state=account.risk_state,
                        valuation_available=True,
                        equity=Decimal(account.equity),
                        risk_available_cash=Decimal(
                            account.risk_available_cash
                        ),
                        risk_ratio=Decimal(account.risk_ratio),
                        warning_ratio=settings.risk_warning_ratio,
                        liquidation_ratio=settings.risk_liquidation_ratio,
                        recovery_ratio=settings.risk_recovery_ratio,
                    ).state
                    final_snapshot = _account_snapshot(account)
                    reconciliation = {
                        "opening_cash": _decimal_text(opening_cash),
                        "trade_cash_flow": _decimal_text(today_trade_cash),
                        "futures_cash_settlement": _decimal_text(
                            futures_holding_pnl
                        ),
                        "option_expiry_cash_flow": _decimal_text(
                            expiry_cash
                        ),
                        "ending_cash": _decimal_text(ending_cash),
                        "position_count": len(account_positions),
                        "trade_count": len(
                            [
                                item
                                for item in account_trades
                                if item.trading_day == trading_day
                            ]
                        ),
                    }
                    result = DailyAccountSettlement(
                        batch_id=batch.batch_id,
                        trading_day=trading_day,
                        account_id=account_id,
                        status=DailySettlementAccountStatus.COMPLETED.value,
                        cash_balance_before=expected_before_cash,
                        opening_cash_balance=opening_cash,
                        cash_balance_after=ending_cash,
                        futures_settlement_pnl=futures_holding_pnl,
                        option_expiry_cash_flow=expiry_cash,
                        trade_cash_flow=today_trade_cash,
                        futures_close_pnl=futures_close_pnl,
                        option_economic_pnl=option_economic_pnl,
                        option_premium_cash_flow=option_premium_cash,
                        daily_close_pnl=total_close_pnl,
                        daily_net_pnl=account.daily_pnl,
                        daily_commission=day_commission,
                        used_commission=account.used_commission,
                        realized_pnl=account.realized_pnl,
                        used_margin=account.used_margin,
                        option_used_margin=account.option_used_margin,
                        frozen_margin=account.frozen_margin,
                        frozen_cash=account.frozen_cash,
                        frozen_commission=account.frozen_commission,
                        long_option_market_value=(
                            account.long_option_market_value
                        ),
                        short_option_market_value=(
                            account.short_option_market_value
                        ),
                        net_option_market_value=(
                            account.net_option_market_value
                        ),
                        equity=account.equity,
                        available_cash=account.available_cash,
                        risk_available_cash=account.risk_available_cash,
                        risk_ratio=account.risk_ratio,
                        risk_state=account.risk_state,
                        before_snapshot=before_account,
                        after_snapshot=final_snapshot,
                        reconciliation_snapshot=reconciliation,
                        started_at=now,
                        settled_at=now,
                    )
                    self.repository.add(db, result)
                    account_results.append(result)

                    account.daily_position_pnl = ZERO
                    account.daily_close_pnl = ZERO
                    account.daily_commission = ZERO
                    account.daily_pnl = ZERO
                    account.trading_day = next_trading_day
                    account.updated_at = now
                    events.create_account_updated(
                        db,
                        account=account,
                        occurred_at=now,
                        fact_reason="DAILY_SETTLEMENT_REPLAY",
                    )

                for position in affected_positions:
                    events.create_position_updated(
                        db,
                        position=position,
                        occurred_at=now,
                        fact_reason="DAILY_SETTLEMENT_REPLAY",
                    )

                if len(account_results) != len(accounts):
                    raise DataAccessError(
                        "账户结算事实数量不守恒",
                        error_code="REPLAY_ACCOUNT_FACT_COUNT_MISMATCH",
                    )
                for item in position_results:
                    expected_volume = (
                        item.opening_yesterday_volume
                        + item.today_open_volume
                        - item.today_close_volume
                    )
                    actual_volume = (
                        0 if item.expired_closed else item.volume_after
                    )
                    if expected_volume != item.volume_before:
                        raise DataAccessError(
                            "持仓期初加开仓减平仓不等于到期处理前数量",
                            error_code=(
                                "REPLAY_POSITION_FACT_VOLUME_MISMATCH"
                            ),
                        )
                    if actual_volume != item.volume_after:
                        raise DataAccessError(
                            "到期处理后的持仓数量不守恒",
                            error_code="REPLAY_EXPIRY_VOLUME_MISMATCH",
                        )

                db.flush()
                if self.repository.count_active_orders(db):
                    raise DataAccessError(
                        "提交前对账发现活动订单",
                        error_code="REPLAY_ACTIVE_ORDER_BARRIER_FAILED",
                    )
                invalid_positions = int(
                    db.scalar(
                        text(
                            "SELECT count(*) FROM position WHERE "
                            "total_volume <> today_volume + yesterday_volume "
                            "OR available_volume <> total_volume - frozen_volume"
                        )
                    )
                    or 0
                )
                if invalid_positions:
                    raise DataAccessError(
                        "提交前对账发现汇总持仓数量不守恒",
                        error_code="REPLAY_POSITION_AGGREGATE_MISMATCH",
                    )
                invalid_details = int(
                    db.scalar(
                        text(
                            "SELECT count(*) FROM position p LEFT JOIN ("
                            "SELECT position_id, sum(remaining_volume) volume, "
                            "sum(frozen_volume) frozen FROM position_detail "
                            "GROUP BY position_id) d ON d.position_id = "
                            "p.position_id WHERE p.total_volume <> "
                            "coalesce(d.volume, 0) OR p.frozen_volume <> "
                            "coalesce(d.frozen, 0)"
                        )
                    )
                    or 0
                )
                if invalid_details:
                    raise DataAccessError(
                        "提交前对账发现汇总持仓与明细不一致",
                        error_code="REPLAY_POSITION_DETAIL_MISMATCH",
                    )
                event_id = f"EVT-{uuid4().hex.upper()}"
                self.outbox_repository.create_event(
                    db=db,
                    event_id=event_id,
                    aggregate_type="SETTLEMENT_BATCH",
                    aggregate_id=batch.batch_id,
                    event_type="DAILY_SETTLEMENT_COMPLETED",
                    payload={
                        "event_id": event_id,
                        "event_type": "DAILY_SETTLEMENT_COMPLETED",
                        "batch_id": batch.batch_id,
                        "trading_day": trading_day.isoformat(),
                        "next_trading_day": next_trading_day.isoformat(),
                        "accounts_settled": len(accounts),
                        "settled_at": now.isoformat(),
                    },
                    created_at=now,
                )
                db.commit()
                return len(accounts)
            except Exception:
                db.rollback()
                raise

    def _complete_batch(self, trading_day: date) -> DailySettlementBatch:
        now = self._now()
        with self.session_factory() as db:
            batch = self.repository.get_batch(db, trading_day, for_update=True)
            if batch is None:
                raise DataAccessError("结算批次不存在")
            batch.status = DailySettlementBatchStatus.COMPLETED.value
            batch.current_stage = DailySettlementStage.COMPLETED.value
            batch.completed_at = now
            batch.failed_at = None
            batch.failure_code = None
            batch.failure_message = None
            batch.failure_account_id = None
            batch.updated_at = now
            db.commit()
            db.refresh(batch)
            db.expunge(batch)
            return batch

    def _recover_redis(self, batch: DailySettlementBatch) -> tuple[str, str | None]:
        if not self.redis_recovery_enabled:
            return SettlementCacheStatus.PENDING.value, "Redis 恢复已由调用方禁用"
        try:
            active_index = ActiveOrderIndex(redis_client)
            with self.session_factory() as db:
                rebuilt = ActiveOrderRebuildService(
                    order_repository=self.order_repository,
                    active_order_index=active_index,
                    batch_size=settings.active_order_rebuild_batch_size,
                ).rebuild(db)
                active_position_models = db.scalars(
                    select(Position)
                    .where(Position.total_volume > 0)
                    .order_by(Position.id)
                ).all()
                active_positions = [
                    (
                        item.account_id,
                        item.exchange_id,
                        item.symbol,
                        item.position_id,
                    )
                    for item in active_position_models
                ]
                affected = db.execute(
                    select(
                        DailyPositionSettlement.account_id,
                        DailyPositionSettlement.exchange_id,
                        DailyPositionSettlement.symbol,
                        DailyPositionSettlement.position_id,
                        DailyPositionSettlement.expired_closed,
                    ).where(DailyPositionSettlement.batch_id == batch.batch_id)
                ).all()
                affected_account_ids = db.scalars(
                    select(DailyAccountSettlement.account_id).where(
                        DailyAccountSettlement.batch_id == batch.batch_id,
                        DailyAccountSettlement.status
                        == DailySettlementAccountStatus.COMPLETED.value,
                    )
                ).all()
                affected_account_id_set = set(affected_account_ids)
                affected_position_ids = {
                    str(item.position_id) for item in affected
                }
                unexpected_new_positions = [
                    item.position_id
                    for item in active_position_models
                    if item.account_id in affected_account_id_set
                    and item.position_id not in affected_position_ids
                ]
                if unexpected_new_positions:
                    raise RuntimeError(
                        "结算完成后账户已产生新持仓，禁止用旧批次覆盖实时基准: "
                        + ", ".join(sorted(unexpected_new_positions))
                    )
                baseline_position_models = [
                    item
                    for item in active_position_models
                    if item.position_id in affected_position_ids
                ]
                accounts = db.scalars(
                    select(Account)
                    .where(Account.account_id.in_(affected_account_ids))
                    .order_by(Account.id)
                ).all()
                position_facts = {
                    item.position_id: item
                    for item in db.scalars(
                        select(DailyPositionSettlement).where(
                            DailyPositionSettlement.batch_id
                            == batch.batch_id
                        )
                    ).all()
                }
                fact_versions = self.outbox_repository.list_latest_fact_versions(
                    db,
                    account_ids=tuple(affected_account_ids),
                    position_ids=tuple(
                        item.position_id for item in baseline_position_models
                    ),
                )
                cumulative_by_account: dict[str, Decimal] = {}
                position_snapshots: list[PositionRealtimePnl] = []
                for position in baseline_position_models:
                    fact = position_facts.get(position.position_id)
                    if fact is None:
                        raise RuntimeError(
                            "活动持仓缺少当日日终结算事实: "
                            f"{position.position_id}"
                        )
                    cumulative = Decimal(position.unrealized_pnl)
                    cumulative_by_account[position.account_id] = (
                        quantize_money(
                            cumulative_by_account.get(
                                position.account_id, ZERO
                            )
                            + cumulative
                        )
                    )
                    position_snapshots.append(
                        PositionRealtimePnl(
                            position_id=position.position_id,
                            account_id=position.account_id,
                            exchange_id=position.exchange_id,
                            symbol=position.symbol,
                            direction=position.direction,
                            mark_price=Decimal(fact.settlement_price),
                            cumulative_unrealized_pnl=cumulative,
                            daily_position_pnl=ZERO,
                            cash_unrealized_pnl=ZERO,
                            instrument_type=position.instrument_type,
                            option_market_value=Decimal(
                                position.option_market_value
                            ),
                            realtime_required_margin=Decimal(
                                position.realtime_required_margin
                            ),
                            event_time=fact.settled_at,
                            source_event_id=(
                                f"SETTLEMENT:{batch.batch_id}:"
                                f"{position.position_id}"
                            ),
                            trading_day=position.trading_day,
                            updated_at=fact.settled_at,
                            data_source="DAILY_SETTLEMENT_BASELINE",
                            source_position_fact_version=(
                                fact_versions.get(
                                    ("POSITION", position.position_id),
                                    "0",
                                )
                            ),
                        )
                    )
                account_snapshots = [
                    AccountRealtimePnl(
                        account_id=account.account_id,
                        cumulative_unrealized_pnl=(
                            cumulative_by_account.get(account.account_id, ZERO)
                        ),
                        daily_position_pnl=ZERO,
                        daily_close_pnl=ZERO,
                        daily_commission=ZERO,
                        daily_pnl=ZERO,
                        cumulative_net_pnl=Decimal(
                            account.cumulative_net_pnl
                        ),
                        equity=Decimal(account.equity),
                        available_cash=Decimal(account.available_cash),
                        futures_unrealized_pnl=ZERO,
                        option_realtime_required_margin=Decimal(
                            account.option_realtime_required_margin
                        ),
                        long_option_market_value=Decimal(
                            account.long_option_market_value
                        ),
                        short_option_market_value=Decimal(
                            account.short_option_market_value
                        ),
                        net_option_market_value=Decimal(
                            account.net_option_market_value
                        ),
                        risk_available_cash=Decimal(
                            account.risk_available_cash
                        ),
                        risk_state=account.risk_state,
                        risk_ratio=Decimal(account.risk_ratio),
                        updated_at=account.updated_at,
                        data_source="DAILY_SETTLEMENT_BASELINE",
                        source_account_fact_version=fact_versions.get(
                            ("ACCOUNT", account.account_id), "0"
                        ),
                        trading_day=account.trading_day,
                    )
                    for account in accounts
                ]
            if rebuilt.failed:
                raise RuntimeError(f"活动订单索引重建失败 {rebuilt.failed} 项")
            pnl_store = RealtimePnlStore(redis_client)
            pnl_store.rebuild_after_daily_settlement(
                active_positions=active_positions,
                affected_positions=affected,
                affected_account_ids=affected_account_ids,
                position_snapshots=position_snapshots,
                account_snapshots=account_snapshots,
                dirty_version=f"SETTLEMENT:{batch.batch_id}",
            )
            status, message = SettlementCacheStatus.COMPLETED.value, None
        except Exception as exc:
            status, message = SettlementCacheStatus.FAILED.value, str(exc)[:2000]
        with self.session_factory() as db:
            current = self.repository.get_batch(
                db, batch.trading_day, for_update=True
            )
            if current is not None:
                current.cache_status = status
                current.cache_failure_message = message
                current.updated_at = self._now()
                db.commit()
        return status, message

    def _mark_batch_failed(
        self,
        *,
        trading_day: date,
        stage: str,
        exc: Exception,
        account_id: str | None,
    ) -> None:
        with self.session_factory() as db:
            batch = self.repository.get_batch(db, trading_day, for_update=True)
            if batch is None or batch.status == "COMPLETED":
                return
            batch.status = DailySettlementBatchStatus.FAILED.value
            batch.current_stage = stage
            batch.failed_at = self._now()
            batch.failure_code = getattr(exc, "error_code", type(exc).__name__)
            batch.failure_message = str(exc)[:4000]
            batch.failure_account_id = account_id
            batch.updated_at = self._now()
            db.commit()

    def run(self, trading_day: date) -> DailySettlementResult:
        """执行或续跑一个交易日；已完成批次只恢复缓存并幂等返回。"""

        if not isinstance(trading_day, date) or isinstance(trading_day, datetime):
            raise DailySettlementError(
                "trading_day 必须是 date",
                stage=DailySettlementStage.PREFLIGHT.value,
                error_code="INVALID_TRADING_DAY_ARGUMENT",
                retriable=False,
            )
        batch: DailySettlementBatch | None = None
        stage = DailySettlementStage.PREFLIGHT.value
        account_id: str | None = None
        with self.database_engine.connect() as lock_connection:
            self._acquire_exclusive_lock(lock_connection)
            try:
                next_trading_day = self._preflight(trading_day)
                batch = self._load_or_start_batch(trading_day)
                if batch.status == DailySettlementBatchStatus.COMPLETED.value:
                    cache_status, cache_message = self._recover_redis(batch)
                    with self.session_factory() as db:
                        count = self.repository.count_completed_accounts(db, batch.batch_id)
                    return DailySettlementResult(
                        batch.batch_id,
                        trading_day,
                        batch.status,
                        batch.current_stage,
                        count,
                        True,
                        cache_status,
                        cache_message,
                    )

                stage = DailySettlementStage.ORDERS_CANCELLED.value
                self._cancel_active_orders()
                self._advance(trading_day, DailySettlementStage.ORDERS_CANCELLED)

                stage = DailySettlementStage.BARRIER_CONFIRMED.value
                self._confirm_barrier()
                self._advance(trading_day, DailySettlementStage.BARRIER_CONFIRMED)

                stage = DailySettlementStage.PRICES_FROZEN.value
                with self.session_factory() as db:
                    instruments, instruments_by_id = self._load_instruments(
                        db,
                        include_active_orders=False,
                        trading_day=trading_day,
                    )
                prices = self._freeze_prices(
                    batch=batch,
                    trading_day=trading_day,
                    instruments=instruments,
                    instruments_by_id=instruments_by_id,
                )
                self._advance(trading_day, DailySettlementStage.PRICES_FROZEN)

                stage = DailySettlementStage.ACCOUNTS_SETTLED.value
                settled = self._settle_batch_from_facts(
                    batch=batch,
                    trading_day=trading_day,
                    next_trading_day=next_trading_day,
                    instruments=instruments,
                    instruments_by_id=instruments_by_id,
                    prices=prices,
                )
                account_id = None
                self._advance(trading_day, DailySettlementStage.ACCOUNTS_SETTLED)

                stage = DailySettlementStage.RECONCILED.value
                self._advance(trading_day, DailySettlementStage.RECONCILED)
                batch = self._complete_batch(trading_day)
                cache_status, cache_message = self._recover_redis(batch)
                return DailySettlementResult(
                    batch.batch_id,
                    trading_day,
                    batch.status,
                    batch.current_stage,
                    settled,
                    False,
                    cache_status,
                    cache_message,
                )
            except DailySettlementError as exc:
                if batch is not None:
                    self._mark_batch_failed(
                        trading_day=trading_day,
                        stage=exc.stage,
                        exc=exc,
                        account_id=exc.account_id,
                    )
                    exc.batch_id = exc.batch_id or batch.batch_id
                raise
            except Exception as exc:
                if batch is not None:
                    self._mark_batch_failed(
                        trading_day=trading_day,
                        stage=stage,
                        exc=exc,
                        account_id=account_id,
                    )
                raise DailySettlementError(
                    str(exc),
                    stage=stage,
                    error_code=getattr(exc, "error_code", "DAILY_SETTLEMENT_FAILED"),
                    batch_id=batch.batch_id if batch else None,
                    account_id=account_id,
                ) from exc
            finally:
                self._release_exclusive_lock(lock_connection)
