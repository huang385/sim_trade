import logging
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.common.time_utils import utc_now
from app.core.config import settings
from app.enums.account_enums import AccountRiskState
from app.enums.risk_enums import LiquidationTaskStatus, RiskEventType
from app.models.liquidation_task import LiquidationTask
from app.repositories.account_repository import AccountRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.risk_repository import RiskRepository
from app.services.account_access_scope import AccountAccessScope
from app.services.account_risk_state_service import AccountRiskStateService
from app.services.order_cancellation_service import OrderCancellationService
from app.services.risk_event_service import RiskEventService
from app.services.settlement_gate_service import SettlementGateService
from app.services.pnl_snapshot_persistence_service import (
    PnlSnapshotPersistenceService,
)
from app.schemas.order_schema import OrderCancelRequest


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskMonitorResult:
    account_id: str
    state: str
    changed: bool = False
    open_orders_cancelled: int = 0
    liquidation_task_id: str | None = None
    retain_dirty: bool = False


class RiskMonitorService:
    """对单账户执行风险复核、先撤开仓挂单、再创建幂等强平任务。"""

    def __init__(
        self,
        *,
        session_factory,
        cancellation_service: OrderCancellationService,
        account_repository: AccountRepository | None = None,
        order_repository: OrderRepository | None = None,
        risk_repository: RiskRepository | None = None,
        event_service: RiskEventService | None = None,
        revaluation_service: PnlSnapshotPersistenceService | None = None,
        settlement_gate_service: SettlementGateService | None = None,
    ):
        self.session_factory = session_factory
        self.cancellation_service = cancellation_service
        self.account_repository = account_repository or AccountRepository()
        self.order_repository = order_repository or OrderRepository()
        self.risk_repository = risk_repository or RiskRepository()
        self.event_service = event_service or RiskEventService(
            risk_repository=self.risk_repository
        )
        self.revaluation_service = revaluation_service
        self.settlement_gate_service = (
            settlement_gate_service or SettlementGateService()
        )

    @staticmethod
    def _valuation_available(account) -> bool:
        return account.risk_state != AccountRiskState.VALUATION_UNAVAILABLE.value

    def _assess_locked(self, account):
        return AccountRiskStateService.evaluate(
            current_state=account.risk_state,
            valuation_available=self._valuation_available(account),
            equity=Decimal(account.equity),
            risk_available_cash=Decimal(account.risk_available_cash),
            risk_ratio=Decimal(account.risk_ratio),
            warning_ratio=settings.risk_warning_ratio,
            liquidation_ratio=settings.risk_liquidation_ratio,
            recovery_ratio=settings.risk_recovery_ratio,
        )

    def _transition(self, db, account, *, state: str, reason: str) -> bool:
        if account.risk_state == state:
            return False
        previous = account.risk_state
        account.risk_state = state
        account.updated_at = utc_now()
        event_type = (
            RiskEventType.WARNING
            if state == AccountRiskState.WARNING.value
            else RiskEventType.STATE_CHANGED
        )
        self.event_service.record(
            db,
            account=account,
            event_type=event_type,
            previous_state=previous,
            trigger_reason=reason,
            occurred_at=account.updated_at,
        )
        logger.info(
            "账户风险状态变化 account_id=%s previous=%s current=%s reason=%s",
            account.account_id,
            previous,
            state,
            reason,
        )
        return True

    def _evaluate_and_commit(self, account_id: str) -> tuple[str, bool, str]:
        with self.session_factory() as db:
            try:
                self.settlement_gate_service.ensure_trading_open(db)
                account = self.account_repository.get_by_account_id_for_update(
                    db, account_id
                )
                if account is None:
                    db.rollback()
                    return "MISSING", False, "ACCOUNT_NOT_FOUND"
                decision = self._assess_locked(account)
                changed = self._transition(
                    db, account, state=decision.state, reason=decision.reason
                )
                db.commit()
                return decision.state, changed, decision.reason
            except Exception:
                db.rollback()
                raise

    def _cancel_open_orders(self, account_id: str) -> int:
        with self.session_factory() as db:
            order_ids = [
                order.order_id
                for order in self.order_repository.list_active_open_by_account(
                    db, account_id
                )
            ]
            db.rollback()
        cancelled = 0
        for order_id in order_ids:
            with self.session_factory() as db:
                order = self.cancellation_service.cancel_order(
                    db=db,
                    order_id=order_id,
                    request=OrderCancelRequest(account_id=account_id),
                    access_scope=AccountAccessScope.admin(),
                )
                if order.status in {"CANCELLED", "PARTIALLY_CANCELLED"}:
                    cancelled += 1
        return cancelled

    def _create_task(self, account_id: str, reason: str) -> str:
        with self.session_factory() as db:
            try:
                self.settlement_gate_service.ensure_trading_open(db)
                account = self.account_repository.get_by_account_id_for_update(
                    db, account_id
                )
                if account is None:
                    db.rollback()
                    return ""
                existing = self.risk_repository.get_active_task_for_update(
                    db, account_id
                )
                if existing is not None:
                    db.commit()
                    return existing.task_id
                task_id = f"LQ-{uuid4().hex.upper()}"
                task = LiquidationTask(
                    task_id=task_id,
                    account_id=account_id,
                    active_key=account_id,
                    trigger_reason=reason,
                    trigger_snapshot=self.event_service.snapshot(account),
                    status=LiquidationTaskStatus.PENDING.value,
                    version=1,
                    retry_count=0,
                    created_at=utc_now(),
                )
                self.risk_repository.add_task(db, task)
                previous = account.risk_state
                account.risk_state = AccountRiskState.LIQUIDATION_PENDING.value
                account.updated_at = utc_now()
                self.event_service.record(
                    db,
                    account=account,
                    event_type=RiskEventType.LIQUIDATION_STARTED,
                    previous_state=previous,
                    trigger_reason=reason,
                    occurred_at=account.updated_at,
                    extra={"task_id": task_id},
                )
                db.commit()
                return task_id
            except IntegrityError:
                db.rollback()
                with self.session_factory() as retry_db:
                    self.settlement_gate_service.ensure_trading_open(retry_db)
                    existing = self.risk_repository.get_active_task_for_update(
                        retry_db, account_id
                    )
                    retry_db.commit()
                    return existing.task_id if existing else ""
            except Exception:
                db.rollback()
                raise

    def process_account(self, account_id: str) -> RiskMonitorResult:
        state, changed, reason = self._evaluate_and_commit(account_id)
        if state == "MISSING":
            return RiskMonitorResult(account_id=account_id, state=state)
        if state == AccountRiskState.VALUATION_UNAVAILABLE.value:
            return RiskMonitorResult(
                account_id=account_id,
                state=state,
                changed=changed,
                retain_dirty=True,
            )
        if state in {
            AccountRiskState.LIQUIDATION_PENDING.value,
            AccountRiskState.LIQUIDATING.value,
        }:
            return RiskMonitorResult(
                account_id=account_id,
                state=state,
                changed=changed,
                retain_dirty=True,
            )
        if state != AccountRiskState.MARGIN_DEFICIT.value:
            return RiskMonitorResult(account_id=account_id, state=state, changed=changed)

        # 先提交禁止OPEN的风险状态，再逐笔复用现有撤单事务，避免持有账户锁时反向锁订单。
        cancelled = self._cancel_open_orders(account_id)
        # 撤单改变冻结资金后必须基于最新PostgreSQL持仓与行情立即完整重估，
        # 不能拿撤单前的risk_available_cash误创建强平任务。单元测试可不注入，
        # 正式Worker始终提供该服务。
        if cancelled and self.revaluation_service is not None:
            self.revaluation_service.recalculate_account(account_id)
        post_state, post_changed, post_reason = self._evaluate_and_commit(account_id)
        if post_state != AccountRiskState.MARGIN_DEFICIT.value:
            return RiskMonitorResult(
                account_id=account_id,
                state=post_state,
                changed=changed or post_changed,
                open_orders_cancelled=cancelled,
            )
        task_id = self._create_task(account_id, post_reason or reason)
        return RiskMonitorResult(
            account_id=account_id,
            state=AccountRiskState.LIQUIDATION_PENDING.value,
            changed=True,
            open_orders_cancelled=cancelled,
            liquidation_task_id=task_id or None,
            retain_dirty=True,
        )
