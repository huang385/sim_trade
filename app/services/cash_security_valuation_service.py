"""Durable real-time valuation for STOCK and CONVERTIBLE_BOND only."""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.common.decimal_utils import quantize_money
from app.common.time_utils import utc_now
from app.enums.account_enums import AccountRiskState
from app.enums.instrument_enums import InstrumentType
from app.infrastructure.cash_security_valuation_store import CashSecurityValuationStore
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.models.account import Account
from app.models.position import Position
from app.repositories.outbox_repository import OutboxRepository
from app.services.realtime_fact_event_service import RealtimeFactEventService


CASH_TYPES = (InstrumentType.STOCK.value, InstrumentType.CONVERTIBLE_BOND.value)
CASH_ACCOUNT_TYPES = ("SECURITIES_CASH", "STOCK")
ZERO = Decimal("0")


@dataclass(frozen=True)
class CashSecurityValuationResult:
    requested: int = 0
    persisted: int = 0
    retained: int = 0


class CashSecurityValuationService:
    """Single PostgreSQL writer for cash-security market-value facts."""

    def __init__(self, *, session_factory, store: CashSecurityValuationStore, market_tick_store: MarketTickStore) -> None:
        self.session_factory = session_factory
        self.store = store
        self.market_tick_store = market_tick_store

    @staticmethod
    def _is_cash_position(position: Position) -> bool:
        return (
            position.instrument_type in CASH_TYPES
            and position.direction == "LONG"
            and position.total_volume > 0
        )

    def rebuild_active_index(self) -> None:
        with self.session_factory() as db:
            rows = db.execute(
                select(Position.position_id, Position.account_id, Position.exchange_id, Position.order_book_id)
                .join(Account, Account.account_id == Position.account_id)
                .where(
                    Position.instrument_type.in_(CASH_TYPES), Position.direction == "LONG",
                    Position.total_volume > 0, Account.account_type.in_(CASH_ACCOUNT_TYPES),
                ).order_by(Position.id)
            ).all()
        self.store.rebuild_active_positions(rows)

    def mark_tick_dirty(self, *, exchange_id: str, order_book_id: str, source_event_id: str) -> dict[str, str]:
        return self.store.mark_accounts_dirty(
            self.store.account_ids_for_tick(exchange_id=exchange_id, order_book_id=order_book_id),
            reason=f"TICK:{source_event_id}",
        )

    def mark_account_dirty(self, *, account_id: str, source_event_id: str) -> dict[str, str]:
        return self.store.mark_accounts_dirty((account_id,), reason=f"FACT:{source_event_id}")

    def _mark_from_latest(self, values: dict[str, str], *, expected_day):
        if not values:
            return None
        try:
            tick = MarketTickStore.mapping_to_tick(values)
        except Exception:
            return None
        if (
            tick.last_price is None or not tick.last_price.is_finite() or tick.last_price < ZERO
            or tick.event_time is None or (expected_day is not None and tick.trading_day != expected_day)
        ):
            return None
        return tick

    def _recalculate_locked_account(self, db, account: Account) -> bool:
        positions = db.scalars(
            select(Position).where(
                Position.account_id == account.account_id,
                Position.instrument_type.in_(CASH_TYPES), Position.direction == "LONG",
                Position.total_volume > 0,
            ).order_by(Position.id).with_for_update()
        ).all()
        latest = self.market_tick_store.get_latest_many(
            {(item.exchange_id, item.order_book_id) for item in positions}
        )
        resolved: list[tuple[Position, object]] = []
        for position in positions:
            tick = self._mark_from_latest(
                latest.get((position.exchange_id.strip().upper(), position.order_book_id.strip().upper()), {}),
                expected_day=account.trading_day,
            )
            if tick is None:
                account.risk_state = AccountRiskState.VALUATION_UNAVAILABLE.value
                account.updated_at = utc_now()
                return False
            # A late tick must never roll a durable mark backwards.
            if position.mark_time is not None and tick.event_time < position.mark_time:
                tick = None
            if tick is not None:
                resolved.append((position, tick))
        if len(resolved) != len(positions):
            return False

        stock_value = unrealized = daily_position = ZERO
        now = utc_now()
        events = RealtimeFactEventService(repository=OutboxRepository())
        for position, tick in resolved:
            market_value = quantize_money(tick.last_price * Decimal(position.total_volume) * Decimal(position.multiplier_snapshot))
            # Pre-migration records are safe to value as from their cost until
            # the first EOD rolls an explicit daily baseline.
            basis = Decimal(position.daily_pnl_base_cost or position.position_cost)
            position.market_value = market_value
            position.mark_price = tick.last_price
            position.mark_time = tick.event_time
            position.mark_source_event_id = tick.source_event_id
            position.unrealized_pnl = quantize_money(market_value - Decimal(position.position_cost))
            position.daily_position_pnl = quantize_money(market_value - basis)
            position.updated_at = now
            stock_value += market_value
            unrealized += position.unrealized_pnl
            daily_position += position.daily_position_pnl
            events.create_position_updated(db, position=position, occurred_at=now, fact_reason="CASH_SECURITY_REALTIME_VALUATION")

        account.stock_market_value = quantize_money(stock_value)
        account.unrealized_pnl = quantize_money(unrealized)
        account.daily_position_pnl = quantize_money(daily_position)
        account.daily_pnl = quantize_money(account.daily_position_pnl + Decimal(account.daily_close_pnl) - Decimal(account.daily_commission))
        account.cumulative_net_pnl = quantize_money(Decimal(account.realized_pnl) + account.unrealized_pnl - Decimal(account.used_commission))
        account.equity = quantize_money(Decimal(account.cash_balance) + account.stock_market_value)
        # Cash accounts do not gain buying power from unrealised securities.
        account.risk_available_cash = Decimal(account.available_cash)
        account.risk_ratio = ZERO
        account.risk_state = AccountRiskState.NORMAL.value
        account.updated_at = now
        events.create_account_updated(db, account=account, occurred_at=now, account_type="SECURITIES_CASH", fact_reason="CASH_SECURITY_REALTIME_VALUATION", include_valuation_fields=True)
        return True

    def persist_batch(self, batch_size: int = 100) -> CashSecurityValuationResult:
        dirty = self.store.list_dirty_accounts(batch_size)
        persisted = retained = 0
        for account_id, version in dirty:
            complete = False
            with self.session_factory() as db:
                try:
                    account = db.scalar(select(Account).where(Account.account_id == account_id).with_for_update())
                    if account is None or account.account_type not in CASH_ACCOUNT_TYPES:
                        complete = True
                    else:
                        complete = self._recalculate_locked_account(db, account)
                    db.commit()
                except Exception:
                    db.rollback()
                    complete = False
            if complete and self.store.complete_dirty_account(account_id=account_id, expected_version=version):
                persisted += 1
            else:
                retained += 1
        return CashSecurityValuationResult(requested=len(dirty), persisted=persisted, retained=retained)
