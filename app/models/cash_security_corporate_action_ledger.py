from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class CashSecurityCorporateActionLedger(Base):
    """公司行为产生的不可变资金与持仓流水。

    此表只追加，不通过更新或删除回滚已发生事实；冲正必须新建反向流水。
    idempotency_key 防止日结重跑、消息重试或并发 Worker 重复派发。
    """
    __tablename__ = "cash_security_corporate_action_ledger"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_cash_corporate_ledger_idempotency"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ledger_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("cash_security_corporate_action.action_id", ondelete="RESTRICT"), nullable=False, index=True)
    component_id: Mapped[str] = mapped_column(ForeignKey("cash_security_corporate_action_component.component_id", ondelete="RESTRICT"), nullable=False)
    entitlement_id: Mapped[str] = mapped_column(ForeignKey("cash_security_corporate_action_entitlement.entitlement_id", ondelete="RESTRICT"), nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.account_id", ondelete="RESTRICT"), nullable=False, index=True)
    position_id: Mapped[str | None] = mapped_column(ForeignKey("position.position_id", ondelete="RESTRICT"), nullable=True)
    entry_type: Mapped[str] = mapped_column(String(48), nullable=False)
    cash_delta: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    receivable_delta: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    position_volume_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_volume_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    position_cost_delta: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    corporate_action_income_delta: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    # 执行时的业务版本/来源版本快照，便于同一 action 多次改版后的审计。
    business_version: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(192), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
