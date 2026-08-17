import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from app.common.time_utils import utc_now
from app.enums.account_enums import AccountRiskState
from app.enums.order_enums import OffsetFlag, OrderDirection, OrderStatus, PositionDirection
from app.enums.product_enums import ProductFamily
from app.enums.risk_enums import LiquidationTaskStatus, OrderSource, RiskEventType
from app.infrastructure.market_data.market_tick_store import MarketTickStore
from app.repositories.account_repository import AccountRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.position_repository import PositionRepository
from app.repositories.risk_repository import RiskRepository
from app.schemas.market_tick_schema import MarketTick
from app.schemas.order_schema import OrderCreateRequest
from app.services.account_access_scope import AccountAccessScope
from app.services.order_cancellation_service import OrderCancellationService
from app.services.order_service import OrderService
from app.schemas.order_schema import OrderCancelRequest
from app.services.risk_event_service import RiskEventService
from app.services.settlement_gate_service import SettlementGateService
from app.services.product_strategy_registry import resolve_product_strategy


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiquidationCandidate:
    position_id: str
    exchange_id: str
    symbol: str
    direction: str
    volume: int
    estimated_release_per_lot: Decimal


@dataclass(frozen=True)
class LiquidationPreparation:
    """锁内生成、锁外执行的强平动作快照，不携带跨Session ORM对象。"""

    action: str
    account_id: str
    candidate: LiquidationCandidate | None = None
    client_order_id: str | None = None
    order_id: str | None = None


class LiquidationService:
    """从数据库持仓事实生成最小合理的reduce_only平仓订单。"""

    ACTIVE_ORDER_STATUSES = {OrderStatus.ACCEPTED.value, OrderStatus.PARTIALLY_FILLED.value}

    def __init__(
        self,
        *,
        session_factory,
        order_service: OrderService,
        cancellation_service: OrderCancellationService,
        market_tick_store: MarketTickStore,
        account_repository: AccountRepository | None = None,
        order_repository: OrderRepository | None = None,
        position_repository: PositionRepository | None = None,
        risk_repository: RiskRepository | None = None,
        event_service: RiskEventService | None = None,
        settlement_gate_service: SettlementGateService | None = None,
        max_retries: int = 10,
    ):
        self.session_factory = session_factory
        self.order_service = order_service
        self.cancellation_service = cancellation_service
        self.market_tick_store = market_tick_store
        self.account_repository = account_repository or AccountRepository()
        self.order_repository = order_repository or OrderRepository()
        self.position_repository = position_repository or PositionRepository()
        self.risk_repository = risk_repository or RiskRepository()
        self.event_service = event_service or RiskEventService(
            risk_repository=self.risk_repository
        )
        self.max_retries = max(max_retries, 1)
        self.settlement_gate_service = (
            settlement_gate_service or SettlementGateService()
        )

    @staticmethod
    def _required_improvement(account) -> Decimal:
        equity = Decimal(account.equity)
        if equity <= 0:
            return max(-Decimal(account.risk_available_cash), Decimal("0"))
        required = Decimal(account.risk_ratio) * equity
        from app.core.config import settings

        return max(
            required - equity * settings.risk_recovery_ratio,
            -Decimal(account.risk_available_cash),
            Decimal("0"),
        )

    @staticmethod
    def _select(rows, required_improvement: Decimal) -> LiquidationCandidate | None:
        # 多头期权可能提供上涨保护，直接平掉会降低权益；有同标的多头保护的
        # 期权空头也暂不自动拆解，避免第一版破坏有效对冲后反而扩大风险。
        protected_underlyings = {
            instrument.underlying_instrument_id
            for position, instrument in rows
            if resolve_product_strategy(position.instrument_type).family
            == ProductFamily.OPTIONS
            and position.direction == PositionDirection.LONG.value
            and instrument.underlying_instrument_id is not None
        }
        candidates: list[LiquidationCandidate] = []
        for position, instrument in rows:
            if position.available_volume <= 0:
                continue
            product_family = resolve_product_strategy(
                position.instrument_type
            ).family
            # 股票现金订单目前只受理与冻结，尚未实现成交、结算或自动强平；
            # 绝不能把它当作期货持仓进入衍生品强平计算。
            if product_family not in {ProductFamily.FUTURES, ProductFamily.OPTIONS}:
                continue
            is_option = product_family == ProductFamily.OPTIONS
            if is_option and position.direction == PositionDirection.LONG.value:
                continue
            if is_option and instrument.underlying_instrument_id in protected_underlyings:
                continue
            release_total = (
                Decimal(position.realtime_required_margin)
                if is_option
                else Decimal(position.used_margin)
            )
            release_per_lot = release_total / Decimal(position.total_volume)
            if release_per_lot <= 0:
                continue
            volume = min(
                position.available_volume,
                max(
                    1,
                    int(
                        (required_improvement / release_per_lot).to_integral_value(
                            rounding=ROUND_CEILING
                        )
                    ),
                ),
            )
            candidates.append(
                LiquidationCandidate(
                    position_id=position.position_id,
                    exchange_id=position.exchange_id,
                    symbol=position.symbol,
                    direction=position.direction,
                    volume=volume,
                    estimated_release_per_lot=release_per_lot,
                )
            )
        return max(candidates, key=lambda item: item.estimated_release_per_lot, default=None)

    def _prepare(self, task_id: str) -> LiquidationPreparation | None:
        with self.session_factory() as db:
            # 先无锁读取账户编号，再统一按Account→LiquidationTask取得行锁。
            # 创建任务同样采用该顺序，避免风险复核与强平执行形成环路死锁。
            task_hint = self.risk_repository.get_task(db, task_id)
            if task_hint is None:
                db.rollback()
                return None
            account_id = task_hint.account_id
            db.rollback()
            self.settlement_gate_service.ensure_trading_open(db)
            account = self.account_repository.get_by_account_id_for_update(
                db, account_id
            )
            task = self.risk_repository.get_task_for_update(db, task_id)
            if task is None or task.status not in {
                LiquidationTaskStatus.PENDING.value,
                LiquidationTaskStatus.LIQUIDATING.value,
            }:
                db.rollback()
                return None
            if account is None:
                task.status = LiquidationTaskStatus.FAILED.value
                task.active_key = None
                task.completed_at = utc_now()
                task.last_error = "ACCOUNT_NOT_FOUND"
                db.commit()
                return None
            if account.risk_state == AccountRiskState.VALUATION_UNAVAILABLE.value:
                # 估值不完整时保留可恢复任务，但禁止产生任何自动强平委托。
                db.commit()
                return None
            existing_orders = self.order_repository.list_by_liquidation_task(
                db, task.task_id
            )
            active_order = next(
                (
                    order
                    for order in existing_orders
                    if order.status in self.ACTIVE_ORDER_STATUSES
                ),
                None,
            )
            if account.risk_state in {
                AccountRiskState.NORMAL.value,
                AccountRiskState.WARNING.value,
                AccountRiskState.RECOVERED.value,
            }:
                if active_order is not None:
                    # 部分成交已使风险恢复时，必须先撤销剩余强平量；否则旧订单
                    # 仍可能继续成交并超过“恢复安全所需的最小减仓量”。
                    result = LiquidationPreparation(
                        action="CANCEL_RECOVERED_ORDER",
                        account_id=account.account_id,
                        order_id=active_order.order_id,
                    )
                    db.commit()
                    return result
                self._complete_locked(db, account, task)
                db.commit()
                return None
            if active_order is not None:
                db.commit()
                return None
            rows = self.position_repository.list_liquidation_candidates(
                db, task.account_id
            )
            candidate = self._select(rows, self._required_improvement(account))
            if candidate is None:
                task.last_error = "NO_SAFE_LIQUIDATION_CANDIDATE"
                task.version += 1
                db.commit()
                return None
            previous = account.risk_state
            account.risk_state = AccountRiskState.LIQUIDATING.value
            account.updated_at = utc_now()
            task.status = LiquidationTaskStatus.LIQUIDATING.value
            task.started_at = task.started_at or account.updated_at
            task.version += 1
            if not task.pending_client_order_id:
                task.pending_client_order_id = (
                    f"LQ-{task.task_id[-20:]}-{task.version:08d}"
                )
            self.event_service.record(
                db,
                account=account,
                event_type=RiskEventType.STATE_CHANGED,
                previous_state=previous,
                trigger_reason="LIQUIDATION_ORDER_PREPARING",
                occurred_at=account.updated_at,
                extra={"task_id": task.task_id},
            )
            client_order_id = task.pending_client_order_id
            db.commit()
            return LiquidationPreparation(
                action="CREATE_ORDER",
                account_id=account_id,
                candidate=candidate,
                client_order_id=client_order_id,
            )

    def _complete_locked(self, db, account, task) -> None:
        """调用方已按Account→Task锁定，在同一事务写任务终态和风险事件。"""

        task.status = LiquidationTaskStatus.COMPLETED.value
        task.active_key = None
        task.completed_at = utc_now()
        task.pending_client_order_id = None
        task.version += 1
        self.event_service.record(
            db,
            account=account,
            event_type=RiskEventType.LIQUIDATION_COMPLETED,
            previous_state=account.risk_state,
            trigger_reason="RISK_RECOVERED",
            occurred_at=task.completed_at,
            extra={"task_id": task.task_id},
        )

    def _complete_after_recovery_cancel(self, task_id: str) -> None:
        with self.session_factory() as db:
            task_hint = self.risk_repository.get_task(db, task_id)
            if task_hint is None:
                db.rollback()
                return
            account_id = task_hint.account_id
            db.rollback()
            self.settlement_gate_service.ensure_trading_open(db)
            account = self.account_repository.get_by_account_id_for_update(
                db, account_id
            )
            task = self.risk_repository.get_task_for_update(db, task_id)
            if account is None or task is None or task.active_key is None:
                db.rollback()
                return
            active_orders = self.order_repository.list_by_liquidation_task(
                db, task_id
            )
            if any(
                order.status in self.ACTIVE_ORDER_STATUSES
                for order in active_orders
            ):
                db.commit()
                return
            if account.risk_state in {
                AccountRiskState.NORMAL.value,
                AccountRiskState.WARNING.value,
                AccountRiskState.RECOVERED.value,
            }:
                self._complete_locked(db, account, task)
            db.commit()

    def execute_task(self, task_id: str) -> str:
        prepared = self._prepare(task_id)
        if prepared is None:
            return "NO_ACTION"
        if prepared.action == "CANCEL_RECOVERED_ORDER":
            try:
                with self.session_factory() as db:
                    self.cancellation_service.cancel_order(
                        db=db,
                        order_id=prepared.order_id or "",
                        request=OrderCancelRequest(
                            account_id=prepared.account_id
                        ),
                        access_scope=AccountAccessScope.admin(),
                    )
                self._complete_after_recovery_cancel(task_id)
                return "ORDER_CANCELLED"
            except Exception as exc:
                self._record_retry(
                    task_id, f"{type(exc).__name__}: {str(exc)[:256]}"
                )
                return "RETRY"
        candidate = prepared.candidate
        if candidate is None or prepared.client_order_id is None:
            self._record_retry(task_id, "INVALID_LIQUIDATION_PREPARATION")
            return "RETRY"
        account_id = prepared.account_id
        values = self.market_tick_store.get_latest(
            candidate.exchange_id, candidate.symbol
        )
        try:
            tick = MarketTick.model_validate(
                {key: None if value == "" else value for key, value in values.items()}
            )
        except Exception:
            self._mark_valuation_unavailable(task_id, "MARKET_PRICE_UNAVAILABLE")
            return "RETRY"
        direction = (
            OrderDirection.SELL
            if candidate.direction == PositionDirection.LONG.value
            else OrderDirection.BUY
        )
        price = tick.bid_price_1 if direction == OrderDirection.SELL else tick.ask_price_1
        if price is None or price <= Decimal("0"):
            self._mark_valuation_unavailable(task_id, "MARKET_PRICE_UNAVAILABLE")
            return "RETRY"
        request = OrderCreateRequest(
            client_order_id=prepared.client_order_id,
            account_id=account_id,
            exchange_id=candidate.exchange_id,
            symbol=candidate.symbol,
            direction=direction,
            offset_flag=OffsetFlag.CLOSE,
            limit_price=price,
            volume=candidate.volume,
        )
        try:
            with self.session_factory() as db:
                order = self.order_service.create_order(
                    db,
                    request,
                    access_scope=AccountAccessScope.admin(),
                    order_source=OrderSource.LIQUIDATION.value,
                    liquidation_task_id=task_id,
                    reduce_only=True,
                )
            self._record_order(task_id, order.order_id)
            logger.info(
                "强平订单已创建 task_id=%s account_id=%s order_id=%s",
                task_id,
                account_id,
                order.order_id,
            )
            return "ORDER_CREATED"
        except Exception as exc:
            self._record_retry(task_id, f"{type(exc).__name__}: {str(exc)[:256]}")
            return "RETRY"

    def _record_retry(self, task_id: str, error: str) -> None:
        with self.session_factory() as db:
            task_hint = self.risk_repository.get_task(db, task_id)
            if task_hint is None:
                db.rollback()
                return
            account_id = task_hint.account_id
            db.rollback()
            self.settlement_gate_service.ensure_trading_open(db)
            account = self.account_repository.get_by_account_id_for_update(
                db, account_id
            )
            task = self.risk_repository.get_task_for_update(db, task_id)
            if account is None or task is None or task.active_key is None:
                db.rollback()
                return
            task.retry_count += 1
            task.last_error = error
            task.version += 1
            if task.retry_count < self.max_retries:
                task.status = LiquidationTaskStatus.PENDING.value
                db.commit()
                return

            # 连续业务/数据库提交失败达到上限后终止本任务并可靠告警。
            # 账户回到MARGIN_DEFICIT，仍禁止OPEN；后续风险Dirty可创建新的
            # 可审计任务，而不会让账户停留在无活动任务的LIQUIDATING状态。
            previous = account.risk_state
            account.risk_state = AccountRiskState.MARGIN_DEFICIT.value
            account.updated_at = utc_now()
            task.status = LiquidationTaskStatus.FAILED.value
            task.active_key = None
            task.completed_at = account.updated_at
            task.pending_client_order_id = None
            self.event_service.record(
                db,
                account=account,
                event_type=RiskEventType.LIQUIDATION_FAILED,
                previous_state=previous,
                trigger_reason="LIQUIDATION_RETRY_EXHAUSTED",
                occurred_at=account.updated_at,
                extra={"task_id": task_id, "error": error},
            )
            db.commit()

    def _mark_valuation_unavailable(self, task_id: str, error: str) -> None:
        """缺少可靠强平价时原子转为估值不可用，等待PnL行情恢复后复核。"""

        with self.session_factory() as db:
            task_hint = self.risk_repository.get_task(db, task_id)
            if task_hint is None:
                db.rollback()
                return
            account_id = task_hint.account_id
            db.rollback()
            self.settlement_gate_service.ensure_trading_open(db)
            account = self.account_repository.get_by_account_id_for_update(
                db, account_id
            )
            task = self.risk_repository.get_task_for_update(db, task_id)
            if account is None or task is None or task.active_key is None:
                db.rollback()
                return
            previous = account.risk_state
            account.risk_state = AccountRiskState.VALUATION_UNAVAILABLE.value
            account.updated_at = utc_now()
            task.status = LiquidationTaskStatus.PENDING.value
            task.last_error = error
            task.version += 1
            if previous != account.risk_state:
                self.event_service.record(
                    db,
                    account=account,
                    event_type=RiskEventType.STATE_CHANGED,
                    previous_state=previous,
                    trigger_reason=error,
                    occurred_at=account.updated_at,
                    extra={"task_id": task_id},
                )
            db.commit()

    def _record_order(self, task_id: str, order_id: str) -> None:
        with self.session_factory() as db:
            task_hint = self.risk_repository.get_task(db, task_id)
            if task_hint is None:
                db.rollback()
                return
            account_id = task_hint.account_id
            db.rollback()
            self.settlement_gate_service.ensure_trading_open(db)
            account = self.account_repository.get_by_account_id_for_update(
                db, account_id
            )
            task = self.risk_repository.get_task_for_update(db, task_id)
            if account is None or task is None:
                db.rollback()
                return
            if task.last_order_id == order_id and not task.pending_client_order_id:
                db.commit()
                return
            task.last_error = None
            task.retry_count = 0
            task.last_order_id = order_id
            task.pending_client_order_id = None
            task.version += 1
            self.event_service.record(
                db,
                account=account,
                event_type=RiskEventType.LIQUIDATION_ORDER_UPDATED,
                previous_state=account.risk_state,
                trigger_reason="LIQUIDATION_ORDER_CREATED",
                occurred_at=utc_now(),
                extra={"task_id": task_id, "order_id": order_id},
            )
            db.commit()
