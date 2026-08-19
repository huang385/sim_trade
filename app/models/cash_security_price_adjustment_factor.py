from datetime import datetime
from decimal import Decimal
from datetime import date

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


class CashSecurityPriceAdjustmentFactor(Base):
    """除权除息后的展示与分析复权因子。

    它不参与订单校验、撮合或成交；交易链路始终使用交易所原始未复权价格。
    """
    __tablename__ = "cash_security_price_adjustment_factor"
    __table_args__ = (
        UniqueConstraint("instrument_id", "trading_day", "action_id", name="uq_cash_price_adjustment_factor"),
        CheckConstraint("raw_previous_close > 0 AND official_ex_reference_price > 0 AND forward_adjustment_factor > 0 AND backward_adjustment_factor > 0", name="ck_cash_price_adjustment_factor_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id", ondelete="RESTRICT"), nullable=False, index=True)
    trading_day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("cash_security_corporate_action.action_id", ondelete="RESTRICT"), nullable=False)
    raw_previous_close: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    # 权威数据源给出的除权参考价，而非系统使用公式猜测并覆盖行情事实。
    official_ex_reference_price: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    forward_adjustment_factor: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    backward_adjustment_factor: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    data_source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
