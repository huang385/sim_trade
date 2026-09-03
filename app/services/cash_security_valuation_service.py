"""仅用于股票和可转债的、可持久化的实时估值服务。"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.common.decimal_utils import quantize_money
from app.common.time_utils import utc_now
from app.core.config import settings
from app.enums.account_enums import AccountRiskState
from app.enums.instrument_enums import CASH_SECURITY_INSTRUMENT_TYPES
from app.infrastructure.cash_security_valuation_store import CashSecurityValuationStore
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.infrastructure.realtime_pnl_store import RealtimePnlStore
from app.models.account import Account
from app.models.position import Position
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.cash_security_valuation_fence_repository import (
    CashSecurityValuationFenceRepository,
)
from app.infrastructure.redis_keys import CASH_VALUATION_WORKER_LEASE_KEY
from app.schemas.pnl_schema import AccountRealtimePnl, PositionRealtimePnl


CASH_TYPES = tuple(sorted(CASH_SECURITY_INSTRUMENT_TYPES))
CASH_ACCOUNT_TYPES = ("SECURITIES_CASH", "STOCK")
ZERO = Decimal("0")


@dataclass(frozen=True)
class CashSecurityValuationResult:
    requested: int = 0
    persisted: int = 0
    retained: int = 0


class CashSecurityValuationService:
    """现金证券市值事实的 PostgreSQL 单写者，避免并发估值互相覆盖。"""

    def __init__(
        self,
        *,
        session_factory,
        store: CashSecurityValuationStore,
        market_tick_store: MarketTickStore,
        pnl_store: RealtimePnlStore | None = None,
        fence_repository: CashSecurityValuationFenceRepository | None = None,
        tick_max_age_seconds: int | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.store = store
        self.market_tick_store = market_tick_store
        self.pnl_store = pnl_store or RealtimePnlStore(store.redis_client)
        self.fence_repository = fence_repository or CashSecurityValuationFenceRepository()
        self.tick_max_age_seconds = (
            tick_max_age_seconds
            if tick_max_age_seconds is not None
            else settings.cash_security_valuation_tick_max_age_seconds
        )

    @staticmethod
    def _is_cash_position(position: Position) -> bool:
        return (
            position.instrument_type in CASH_TYPES
            and position.direction == "LONG"
            and position.total_volume > 0
        )

    def rebuild_active_index(self) -> None:
        # Redis 索引只是“某行情会影响哪些账户”的可重建路由表；持仓表才是
        # 权威来源。进程重启、成交或日终结算后都可以安全地从数据库重建。
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

    def activate_writer_fence(self, *, owner: str, fencing_token: str) -> bool:
        """Publish a Redis-issued epoch to the PostgreSQL write resource."""

        with self.session_factory() as db:
            try:
                activated = self.fence_repository.activate(
                    db, owner=owner, fencing_token=fencing_token
                )
                if activated:
                    db.commit()
                else:
                    db.rollback()
                return activated
            except Exception:
                db.rollback()
                raise

    def refresh_active_index_for_position(self, position_id: str) -> None:
        """Incrementally refresh one routing row after a committed position fact."""

        with self.session_factory() as db:
            position = db.scalar(
                select(Position).where(Position.position_id == position_id)
            )
            if position is None or not self._is_cash_position(position):
                self.store.remove_active_position(position_id)
                return
            account = db.scalar(
                select(Account).where(Account.account_id == position.account_id)
            )
            if account is None or account.account_type not in CASH_ACCOUNT_TYPES:
                self.store.remove_active_position(position_id)
                return
            self.store.upsert_active_position(
                position_id=position.position_id,
                account_id=position.account_id,
                exchange_id=position.exchange_id,
                order_book_id=position.order_book_id,
            )

    def _mark_from_latest(self, values: dict[str, str], *, expected_day):
        if not values:
            return None
        try:
            tick = MarketTickStore.mapping_to_tick(values)
        except Exception:
            return None
        # 估值只能使用当前交易日、带有效时间的最新成交价。行情缺失或跨日时
        # 宁可保留待处理状态，也不能把过期价格写成当前市值。
        age_seconds = (utc_now() - tick.event_time).total_seconds() if tick.event_time else None
        if (
            tick.last_price is None or not tick.last_price.is_finite() or tick.last_price <= ZERO
            or tick.event_time is None or not tick.source_event_id
            or (expected_day is not None and tick.trading_day != expected_day)
            or age_seconds is None or age_seconds < 0 or age_seconds > self.tick_max_age_seconds
        ):
            return None
        return tick

    @staticmethod
    def _account_realtime_snapshot(db, account: Account, *, updated_at):
        fact_versions = OutboxRepository().list_latest_fact_versions(
            db, account_ids=(account.account_id,), position_ids=()
        )
        return AccountRealtimePnl(
            account_id=account.account_id,
            cumulative_unrealized_pnl=account.unrealized_pnl,
            daily_position_pnl=account.daily_position_pnl,
            daily_close_pnl=account.daily_close_pnl,
            daily_commission=account.daily_commission,
            daily_pnl=account.daily_pnl,
            cumulative_net_pnl=account.cumulative_net_pnl,
            equity=account.equity,
            stock_market_value=account.stock_market_value,
            corporate_action_receivable=account.corporate_action_receivable,
            pending_security_value=account.pending_security_value,
            available_cash=account.available_cash,
            risk_available_cash=account.risk_available_cash,
            risk_state=account.risk_state,
            risk_ratio=account.risk_ratio,
            updated_at=updated_at,
            trading_day=account.trading_day,
            source_account_fact_version=fact_versions.get(
                ("ACCOUNT", account.account_id), "0"
            ),
        )

    def _recalculate_locked_account(self, db, account: Account):
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
                state_changed = account.risk_state != AccountRiskState.VALUATION_UNAVAILABLE.value
                if state_changed:
                    account.risk_state = AccountRiskState.VALUATION_UNAVAILABLE.value
                    account.updated_at = utc_now()
                    return False, (), self._account_realtime_snapshot(
                        db, account, updated_at=account.updated_at
                    ), True
                return False, (), None, False
            # 乱序到达的旧 Tick 不能把已经持久化的较新盯市价格回滚。
            if position.mark_time is not None and tick.event_time < position.mark_time:
                tick = None
            if tick is not None:
                resolved.append((position, tick))
        if len(resolved) != len(positions):
            return False, (), None, False

        stock_value = pending_security_value = unrealized = daily_position = ZERO
        now = utc_now()
        fact_versions = OutboxRepository().list_latest_fact_versions(
            db,
            account_ids=(account.account_id,),
            position_ids=tuple(position.position_id for position in positions),
        )
        position_snapshots: list[PositionRealtimePnl] = []
        for position, tick in resolved:
            market_value = quantize_money(tick.last_price * Decimal(position.total_volume) * Decimal(position.multiplier_snapshot))
            # 历史迁移前的记录可能尚未保存日内估值基准；首次日终前以持仓
            # 成本作为保守基准，避免凭空产生当日持仓盈亏。
            # An unestablished bucket keeps the historical aggregate basis.
            # A zero aggregate has no reliable day-start fact, so conservatively
            # use carrying cost until the next verified EOD establishes one.
            basis = Decimal(position.daily_pnl_base_cost)
            if not getattr(position, "daily_pnl_base_established", False) and basis == ZERO:
                basis = Decimal(position.position_cost)
            position.market_value = market_value
            position.mark_price = tick.last_price
            position.mark_time = tick.event_time
            position.mark_source_event_id = tick.source_event_id
            position.unrealized_pnl = quantize_money(market_value - Decimal(position.position_cost))
            position.daily_position_pnl = quantize_money(market_value - basis)
            position.updated_at = now
            stock_value += market_value
            # Pending corporate-action shares are economic assets but not
            # tradable holdings.  Revalue them with the same authoritative
            # raw market price; retaining an ex-date pre-close would distort
            # account equity after ex-right/ex-dividend price adjustment.
            pending_security_value += quantize_money(
                Decimal(getattr(position, "pending_share_volume", 0))
                * tick.last_price
                * Decimal(position.multiplier_snapshot)
            )
            unrealized += position.unrealized_pnl
            daily_position += position.daily_position_pnl
            position_snapshots.append(
                PositionRealtimePnl(
                    position_id=position.position_id,
                    account_id=position.account_id,
                    exchange_id=position.exchange_id,
                    symbol=position.symbol,
                    direction=position.direction,
                    mark_price=position.mark_price,
                    market_value=position.market_value,
                    cumulative_unrealized_pnl=position.unrealized_pnl,
                    daily_position_pnl=position.daily_position_pnl,
                    cash_unrealized_pnl=position.unrealized_pnl,
                    instrument_type=position.instrument_type,
                    event_time=position.mark_time,
                    source_event_id=position.mark_source_event_id,
                    trading_day=position.trading_day,
                    updated_at=now,
                    source_position_fact_version=fact_versions.get(
                        ("POSITION", position.position_id), "0"
                    ),
                )
            )

        account.stock_market_value = quantize_money(stock_value)
        account.pending_security_value = quantize_money(pending_security_value)
        account.unrealized_pnl = quantize_money(unrealized)
        account.daily_position_pnl = quantize_money(daily_position)
        account.daily_pnl = quantize_money(account.daily_position_pnl + Decimal(account.daily_close_pnl) - Decimal(account.daily_commission))
        account.cumulative_net_pnl = quantize_money(
            Decimal(account.realized_pnl) + account.unrealized_pnl
            + Decimal(account.corporate_action_income) - Decimal(account.used_commission)
        )
        account.equity = quantize_money(
            Decimal(account.cash_balance) + account.stock_market_value
            + Decimal(account.corporate_action_receivable)
            + Decimal(account.pending_security_value)
        )
        # 现金证券的浮动市值不直接增加可买资金；可用资金仍只由现金决定。
        account.risk_available_cash = Decimal(account.available_cash)
        account.risk_ratio = ZERO
        risk_state_changed = account.risk_state != AccountRiskState.NORMAL.value
        account.risk_state = AccountRiskState.NORMAL.value
        account.updated_at = now
        account_snapshot = self._account_realtime_snapshot(
            db, account, updated_at=now
        )
        return True, tuple(position_snapshots), account_snapshot, risk_state_changed

    def persist_batch(
        self,
        batch_size: int = 100,
        *,
        lease_owner: str | None = None,
        fencing_token: str | None = None,
    ) -> CashSecurityValuationResult:
        dirty = self.store.list_dirty_accounts(batch_size)
        persisted = retained = 0
        for account_id, version in dirty:
            complete = False
            position_snapshots = ()
            account_snapshot = None
            emit_risk_event = False
            with self.session_factory() as db:
                try:
                    if lease_owner is not None and (
                        fencing_token is None
                        or not self.fence_repository.is_current(
                            db, owner=lease_owner, fencing_token=fencing_token
                        )
                    ):
                        break
                    account = db.scalar(select(Account).where(Account.account_id == account_id).with_for_update())
                    if account is None or account.account_type not in CASH_ACCOUNT_TYPES:
                        complete = True
                    else:
                        (
                            complete,
                            position_snapshots,
                            account_snapshot,
                            emit_risk_event,
                        ) = self._recalculate_locked_account(db, account)
                    db.commit()
                except Exception:
                    db.rollback()
                    complete = False
            # 仅当处理期间没有新的脏标记写入时才清除任务。版本不一致说明有
            # 更新行情或事实到达，保留任务以便下一批重新按最新数据估值。
            if account_snapshot is not None:
                try:
                    if lease_owner is None:
                        self.pnl_store.write_cycle_snapshots(
                            positions=position_snapshots, accounts=(account_snapshot,),
                            dirty_version=f"CASH:{version}", active_positions=(),
                            closed_positions=(), mark_dirty=False,
                            emit_risk_events=emit_risk_event,
                        )
                    else:
                        written, _, _ = self.pnl_store.write_cycle_snapshots_if_lease_value_owned(
                            lease_key=CASH_VALUATION_WORKER_LEASE_KEY,
                            lease_value=self.store.writer_lease_value(lease_owner, fencing_token),
                            positions=position_snapshots, accounts=(account_snapshot,),
                            dirty_version=f"CASH:{version}", active_positions=(),
                            closed_positions=(), mark_dirty=False,
                            emit_risk_events=emit_risk_event,
                        )
                        if not written:
                            complete = False
                except Exception:
                    complete = False
            if complete and self.store.complete_dirty_account(account_id=account_id, expected_version=version):
                persisted += 1
            else:
                retained += 1
        return CashSecurityValuationResult(requested=len(dirty), persisted=persisted, retained=retained)
