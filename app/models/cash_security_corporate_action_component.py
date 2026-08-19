from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class CashSecurityCorporateActionComponent(Base):
    """主事件内可独立审计、可独立执行的一个组成部分。

    例如“每 10 股派 2 元并送 1 股”对应同一 action 的 CASH_DIVIDEND
    和 STOCK_DIVIDEND 两条 component，而不是把混合口径写进一个字段。
    """
    __tablename__ = "cash_security_corporate_action_component"
    __table_args__ = (
        UniqueConstraint("component_id", name="uq_cash_corporate_component_id"),
        CheckConstraint("base_quantity > 0", name="ck_cash_corporate_component_base_positive"),
        CheckConstraint("cash_amount >= 0 AND share_ratio >= 0 AND rights_ratio >= 0 AND subscription_price >= 0 AND withholding_tax_rate >= 0 AND withholding_tax_rate <= 1 AND cash_in_lieu_price >= 0", name="ck_cash_corporate_component_amounts_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    component_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_id: Mapped[str] = mapped_column(ForeignKey("cash_security_corporate_action.action_id", ondelete="RESTRICT"), nullable=False, index=True)
    component_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 所有比例统一为“每 base_quantity 单位证券”的口径，避免每股/每十股混用。
    base_quantity: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    cash_amount: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    share_ratio: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False, default=Decimal("0"))
    rights_ratio: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False, default=Decimal("0"))
    subscription_price: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    withholding_tax_rate: Mapped[Decimal] = mapped_column(Numeric(18, 12), nullable=False, default=Decimal("0"))
    # 并股等产生尾股时的公告补偿价；没有该规则时业务层拒绝静默舍弃尾股。
    cash_in_lieu_price: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    rounding_rule: Mapped[str] = mapped_column(String(32), nullable=False, default="FLOOR")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
