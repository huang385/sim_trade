"""增加手工日终结算事实表。

Revision ID: 20260806_0018
Revises: 20260805_0017
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260806_0018"
down_revision = "20260805_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_settlement_batch",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("current_stage", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("failure_account_id", sa.String(64), nullable=True),
        sa.Column("cache_status", sa.String(16), server_default="PENDING", nullable=False),
        sa.Column("cache_failure_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('RUNNING', 'FAILED', 'COMPLETED')", name="ck_daily_settlement_batch_status"),
        sa.CheckConstraint("cache_status IN ('PENDING', 'COMPLETED', 'FAILED')", name="ck_daily_settlement_cache_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", name="uq_daily_settlement_batch_id"),
        sa.UniqueConstraint("trading_day", name="uq_daily_settlement_trading_day"),
    )

    op.create_table(
        "instrument_settlement_price",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("order_book_id", sa.String(64), nullable=False),
        sa.Column("instrument_type", sa.String(32), nullable=False),
        sa.Column("settlement_price", sa.Numeric(24, 6), nullable=False),
        sa.Column("price_source", sa.String(32), nullable=False),
        sa.Column("source_tick_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_tick_trading_day", sa.Date(), nullable=False),
        sa.Column("source_event_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("settlement_price > 0", name="ck_settlement_price_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trading_day", "exchange_id", "symbol", name="uq_instrument_settlement_price_day_contract"),
    )
    op.create_index("ix_instrument_settlement_price_batch", "instrument_settlement_price", ["batch_id", "id"])

    op.create_table(
        "daily_account_settlement",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("cash_balance_before", sa.Numeric(24, 6), nullable=False),
        sa.Column("cash_balance_after", sa.Numeric(24, 6), nullable=True),
        sa.Column("futures_settlement_pnl", sa.Numeric(24, 6), nullable=False),
        sa.Column("option_expiry_cash_flow", sa.Numeric(24, 6), nullable=False),
        sa.Column("daily_commission", sa.Numeric(24, 6), nullable=True),
        sa.Column("used_commission", sa.Numeric(24, 6), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(24, 6), nullable=True),
        sa.Column("used_margin", sa.Numeric(24, 6), nullable=True),
        sa.Column("option_used_margin", sa.Numeric(24, 6), nullable=True),
        sa.Column("frozen_margin", sa.Numeric(24, 6), nullable=True),
        sa.Column("frozen_cash", sa.Numeric(24, 6), nullable=True),
        sa.Column("frozen_commission", sa.Numeric(24, 6), nullable=True),
        sa.Column("long_option_market_value", sa.Numeric(24, 6), nullable=True),
        sa.Column("short_option_market_value", sa.Numeric(24, 6), nullable=True),
        sa.Column("net_option_market_value", sa.Numeric(24, 6), nullable=True),
        sa.Column("equity", sa.Numeric(24, 6), nullable=True),
        sa.Column("available_cash", sa.Numeric(24, 6), nullable=True),
        sa.Column("risk_available_cash", sa.Numeric(24, 6), nullable=True),
        sa.Column("risk_ratio", sa.Numeric(18, 8), nullable=True),
        sa.Column("risk_state", sa.String(32), nullable=True),
        sa.Column("before_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('RUNNING', 'FAILED', 'COMPLETED')", name="ck_daily_account_settlement_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trading_day", "account_id", name="uq_daily_account_settlement_day_account"),
    )
    op.create_index("ix_daily_account_settlement_batch_status", "daily_account_settlement", ["batch_id", "status", "id"])

    op.create_table(
        "daily_position_settlement",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("position_id", sa.String(64), nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("order_book_id", sa.String(64), nullable=False),
        sa.Column("instrument_type", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("multiplier_snapshot", sa.Numeric(24, 6), nullable=False),
        sa.Column("volume_before", sa.Integer(), nullable=False),
        sa.Column("today_volume_before", sa.Integer(), nullable=False),
        sa.Column("yesterday_volume_before", sa.Integer(), nullable=False),
        sa.Column("volume_after", sa.Integer(), nullable=False),
        sa.Column("today_volume_after", sa.Integer(), nullable=False),
        sa.Column("yesterday_volume_after", sa.Integer(), nullable=False),
        sa.Column("previous_settlement_basis", sa.Numeric(24, 6), nullable=True),
        sa.Column("settlement_price", sa.Numeric(24, 6), nullable=False),
        sa.Column("daily_settlement_pnl", sa.Numeric(24, 6), nullable=False),
        sa.Column("settlement_margin", sa.Numeric(24, 6), nullable=False),
        sa.Column("option_market_value", sa.Numeric(24, 6), nullable=False),
        sa.Column("expired_closed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("before_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("volume_before >= 0", name="ck_daily_position_volume_before"),
        sa.CheckConstraint("volume_after >= 0", name="ck_daily_position_volume_after"),
        sa.CheckConstraint("multiplier_snapshot > 0", name="ck_daily_position_multiplier"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trading_day", "account_id", "position_id", name="uq_daily_position_settlement_day_account_position"),
    )
    op.create_index("ix_daily_position_settlement_batch_account", "daily_position_settlement", ["batch_id", "account_id", "id"])

    op.create_table(
        "option_expiry_settlement_detail",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("position_id", sa.String(64), nullable=False),
        sa.Column("option_order_book_id", sa.String(64), nullable=False),
        sa.Column("option_type", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("underlying_order_book_id", sa.String(64), nullable=False),
        sa.Column("underlying_exchange_id", sa.String(32), nullable=False),
        sa.Column("underlying_symbol", sa.String(64), nullable=False),
        sa.Column("underlying_settlement_price", sa.Numeric(24, 6), nullable=False),
        sa.Column("strike_price", sa.Numeric(24, 6), nullable=False),
        sa.Column("multiplier_snapshot", sa.Numeric(24, 6), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("intrinsic_value", sa.Numeric(24, 6), nullable=False),
        sa.Column("gross_cash_amount", sa.Numeric(24, 6), nullable=False),
        sa.Column("cash_flow", sa.Numeric(24, 6), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_option_expiry_quantity_positive"),
        sa.CheckConstraint("intrinsic_value >= 0", name="ck_option_expiry_intrinsic_nonnegative"),
        sa.CheckConstraint("multiplier_snapshot > 0", name="ck_option_expiry_multiplier_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trading_day", "account_id", "position_id", name="uq_option_expiry_settlement_day_account_position"),
    )
    op.create_index("ix_option_expiry_settlement_batch_account", "option_expiry_settlement_detail", ["batch_id", "account_id", "id"])


def downgrade() -> None:
    # 这些表保存不可重建的资金与结算审计事实。为避免误删历史，降级必须由
    # 运维人员先完成独立归档，再显式编写受控迁移；这里故意拒绝自动删除。
    raise RuntimeError("日终结算事实表包含不可逆历史，禁止自动 downgrade")
