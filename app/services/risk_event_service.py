from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session

from app.enums.risk_enums import RiskEventType
from app.models.account import Account
from app.models.risk_event import RiskEvent
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.risk_repository import RiskRepository


def _decimal(value: Decimal) -> str:
    """风险事件中的金额和比率一律保存为Decimal字符串。"""

    return format(Decimal(value), "f")


class RiskEventService:
    """在账户事务内同时持久化风险审计记录和待发布Outbox。"""

    def __init__(
        self,
        *,
        risk_repository: RiskRepository | None = None,
        outbox_repository: OutboxRepository | None = None,
    ):
        self.risk_repository = risk_repository or RiskRepository()
        self.outbox_repository = outbox_repository or OutboxRepository()

    @staticmethod
    def snapshot(account: Account) -> dict[str, str]:
        return {
            "equity": _decimal(account.equity),
            "available_cash": _decimal(account.available_cash),
            "risk_available_cash": _decimal(account.risk_available_cash),
            "used_margin": _decimal(account.used_margin),
            "option_realtime_required_margin": _decimal(
                account.option_realtime_required_margin
            ),
            "frozen_margin": _decimal(account.frozen_margin),
            "frozen_cash": _decimal(account.frozen_cash),
            "frozen_commission": _decimal(account.frozen_commission),
            "risk_ratio": _decimal(account.risk_ratio),
        }

    def record(
        self,
        db: Session,
        *,
        account: Account,
        event_type: RiskEventType,
        previous_state: str | None,
        trigger_reason: str,
        occurred_at: datetime,
        extra: dict | None = None,
    ) -> RiskEvent:
        account.risk_version = int(account.risk_version or 0) + 1
        event_id = f"RISK-{uuid4().hex.upper()}"
        snapshot = self.snapshot(account)
        payload = {
            "event_id": event_id,
            "event_type": event_type.value,
            "account_id": account.account_id,
            "previous_state": previous_state,
            "risk_state": account.risk_state,
            "trigger_reason": trigger_reason,
            "occurred_at": occurred_at.isoformat(),
            "risk_version": account.risk_version,
            # payload显式保留通用字段名；risk_version用于强调这是账户风险
            # 独立版本域，两者值始终相同。
            "business_version": account.risk_version,
            **snapshot,
            **(extra or {}),
        }
        event = RiskEvent(
            event_id=event_id,
            account_id=account.account_id,
            event_type=event_type.value,
            previous_state=previous_state,
            risk_state=account.risk_state,
            trigger_reason=trigger_reason,
            snapshot={**snapshot, **(extra or {})},
            business_version=account.risk_version,
            created_at=occurred_at,
        )
        self.risk_repository.add_event(db, event)
        self.outbox_repository.create_event(
            db=db,
            event_id=event_id,
            aggregate_type="RISK",
            aggregate_id=account.account_id,
            event_type=event_type.value,
            payload=payload,
            created_at=occurred_at,
        )
        return event
