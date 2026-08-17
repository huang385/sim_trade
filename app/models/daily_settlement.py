from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.time_utils import utc_now
from app.core.database import Base


MONEY = Numeric(24, 6)
RATIO = Numeric(18, 8)


class DailySettlementBatch(Base):
    """一个交易日唯一的手工结算批次及其失败恢复游标。"""

    __tablename__ = "daily_settlement_batch"
    __table_args__ = (
        UniqueConstraint("batch_id", name="uq_daily_settlement_batch_id"),
        UniqueConstraint("trading_day", name="uq_daily_settlement_trading_day"),
        CheckConstraint(
            "status IN ('RUNNING', 'FAILED', 'COMPLETED')",
            name="ck_daily_settlement_batch_status",
        ),
        CheckConstraint(
            "cache_status IN ('PENDING', 'COMPLETED', 'FAILED')",
            name="ck_daily_settlement_cache_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    current_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(Text)
    failure_account_id: Mapped[str | None] = mapped_column(String(64))
    cache_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default="PENDING"
    )
    cache_failure_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class InstrumentSettlementPrice(Base):
    """批次内冻结的当日及前一交易日最后 Tick 价格。"""

    __tablename__ = "instrument_settlement_price"
    __table_args__ = (
        UniqueConstraint(
            "trading_day",
            "exchange_id",
            "symbol",
            name="uq_instrument_settlement_price_day_contract",
        ),
        CheckConstraint("settlement_price > 0", name="ck_settlement_price_positive"),
        CheckConstraint(
            "previous_last_price IS NULL OR previous_last_price > 0",
            name="ck_settlement_previous_last_price_positive",
        ),
        Index("ix_instrument_settlement_price_batch", "batch_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    exchange_id: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    order_book_id: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(32), nullable=False)
    settlement_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    price_source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_tick_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_tick_trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    previous_last_price: Mapped[Decimal | None] = mapped_column(MONEY)
    previous_source_tick_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    previous_source_tick_trading_day: Mapped[date | None] = mapped_column(Date)
    previous_source_event_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class DailyAccountSettlement(Base):
    """逐账户资金快照和幂等处理状态，与资金变更同事务提交。"""

    __tablename__ = "daily_account_settlement"
    __table_args__ = (
        UniqueConstraint(
            "trading_day", "account_id", name="uq_daily_account_settlement_day_account"
        ),
        CheckConstraint(
            "status IN ('RUNNING', 'FAILED', 'COMPLETED')",
            name="ck_daily_account_settlement_status",
        ),
        Index("ix_daily_account_settlement_batch_status", "batch_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    cash_balance_before: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    opening_cash_balance: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    cash_balance_after: Mapped[Decimal | None] = mapped_column(MONEY)
    futures_settlement_pnl: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0")
    )
    option_expiry_cash_flow: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0")
    )
    trade_cash_flow: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    futures_close_pnl: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    option_economic_pnl: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    option_premium_cash_flow: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    daily_close_pnl: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    daily_net_pnl: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    daily_commission: Mapped[Decimal | None] = mapped_column(MONEY)
    used_commission: Mapped[Decimal | None] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    used_margin: Mapped[Decimal | None] = mapped_column(MONEY)
    option_used_margin: Mapped[Decimal | None] = mapped_column(MONEY)
    frozen_margin: Mapped[Decimal | None] = mapped_column(MONEY)
    frozen_cash: Mapped[Decimal | None] = mapped_column(MONEY)
    frozen_commission: Mapped[Decimal | None] = mapped_column(MONEY)
    long_option_market_value: Mapped[Decimal | None] = mapped_column(MONEY)
    short_option_market_value: Mapped[Decimal | None] = mapped_column(MONEY)
    net_option_market_value: Mapped[Decimal | None] = mapped_column(MONEY)
    equity: Mapped[Decimal | None] = mapped_column(MONEY)
    available_cash: Mapped[Decimal | None] = mapped_column(MONEY)
    risk_available_cash: Mapped[Decimal | None] = mapped_column(MONEY)
    risk_ratio: Mapped[Decimal | None] = mapped_column(RATIO)
    risk_state: Mapped[str | None] = mapped_column(String(32))
    before_snapshot: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    after_snapshot: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    reconciliation_snapshot: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=dict,
        server_default="{}",
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(Text)


class DailyPositionSettlement(Base):
    """逐持仓日终数量、基准、估值和保证金快照。"""

    __tablename__ = "daily_position_settlement"
    __table_args__ = (
        UniqueConstraint(
            "trading_day",
            "account_id",
            "position_id",
            name="uq_daily_position_settlement_day_account_position",
        ),
        CheckConstraint("volume_before >= 0", name="ck_daily_position_volume_before"),
        CheckConstraint("volume_after >= 0", name="ck_daily_position_volume_after"),
        CheckConstraint("multiplier_snapshot > 0", name="ck_daily_position_multiplier"),
        Index("ix_daily_position_settlement_batch_account", "batch_id", "account_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange_id: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    order_book_id: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    multiplier_snapshot: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    volume_before: Mapped[int] = mapped_column(Integer, nullable=False)
    opening_yesterday_volume: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    today_open_volume: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    today_close_volume: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    today_close_today_volume: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    today_close_yesterday_volume: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    today_volume_before: Mapped[int] = mapped_column(Integer, nullable=False)
    yesterday_volume_before: Mapped[int] = mapped_column(Integer, nullable=False)
    volume_after: Mapped[int] = mapped_column(Integer, nullable=False)
    today_volume_after: Mapped[int] = mapped_column(Integer, nullable=False)
    yesterday_volume_after: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_settlement_basis: Mapped[Decimal | None] = mapped_column(MONEY)
    settlement_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    daily_settlement_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    close_pnl: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    option_economic_pnl: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    commission: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    premium_cash_flow: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    cumulative_economic_pnl: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    settlement_margin: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    option_market_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    expired_closed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    before_snapshot: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    after_snapshot: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OptionExpirySettlementDetail(Base):
    """到期期权现金差额事实；持仓唯一约束阻止重复收付款。"""

    __tablename__ = "option_expiry_settlement_detail"
    __table_args__ = (
        UniqueConstraint(
            "trading_day",
            "account_id",
            "position_id",
            name="uq_option_expiry_settlement_day_account_position",
        ),
        UniqueConstraint(
            "position_id",
            name="uq_option_expiry_settlement_position_once",
        ),
        CheckConstraint("quantity > 0", name="ck_option_expiry_quantity_positive"),
        CheckConstraint("intrinsic_value >= 0", name="ck_option_expiry_intrinsic_nonnegative"),
        CheckConstraint("multiplier_snapshot > 0", name="ck_option_expiry_multiplier_positive"),
        Index("ix_option_expiry_settlement_batch_account", "batch_id", "account_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)
    option_order_book_id: Mapped[str] = mapped_column(String(64), nullable=False)
    option_type: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    underlying_order_book_id: Mapped[str] = mapped_column(String(64), nullable=False)
    underlying_exchange_id: Mapped[str] = mapped_column(String(32), nullable=False)
    underlying_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    underlying_settlement_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    strike_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    multiplier_snapshot: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    intrinsic_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    gross_cash_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cash_flow: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
