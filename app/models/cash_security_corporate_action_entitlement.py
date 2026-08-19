from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class CashSecurityCorporateActionEntitlement(Base):
    """登记日日结后冻结的账户级权益快照。

    它是客户获得分红、送股或配股资格的唯一依据；后续卖出原持仓不改变
    record_quantity，也不能再次创建相同 action/component/account/position 权益。
    """
    __tablename__ = "cash_security_corporate_action_entitlement"
    __table_args__ = (
        UniqueConstraint("action_id", "component_id", "account_id", "position_id", name="uq_cash_corporate_entitlement"),
        CheckConstraint("record_quantity >= 0 AND entitled_cash_gross >= 0 AND withholding_tax >= 0 AND entitled_cash_net >= 0 AND entitled_share_volume >= 0 AND fractional_share >= 0 AND cash_in_lieu >= 0 AND subscribed_volume >= 0 AND subscription_cash >= 0 AND pending_share_volume >= 0 AND credited_share_volume >= 0", name="ck_cash_corporate_entitlement_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entitlement_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("cash_security_corporate_action.action_id", ondelete="RESTRICT"), nullable=False, index=True)
    component_id: Mapped[str] = mapped_column(ForeignKey("cash_security_corporate_action_component.component_id", ondelete="RESTRICT"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("account.account_id", ondelete="RESTRICT"), nullable=False, index=True)
    position_id: Mapped[str] = mapped_column(ForeignKey("position.position_id", ondelete="RESTRICT"), nullable=False)
    # 登记日屏障内的 PostgreSQL Position.total_volume 快照，绝不读取 Redis。
    record_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entitled_cash_gross: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    withholding_tax: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    entitled_cash_net: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    entitled_share_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fractional_share: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False, default=Decimal("0"))
    cash_in_lieu: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    subscribed_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subscription_cash: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False, default=Decimal("0"))
    # 已取得经济权益、但尚未上市/尚不可交易的股份。
    pending_share_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credited_share_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    record_position_version: Mapped[str] = mapped_column(String(128), nullable=False)
    # 配股认购请求幂等键；普通用户不能借此绕过账户授权检查。
    client_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
