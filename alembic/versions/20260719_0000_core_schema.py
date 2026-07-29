"""建立Alembic接管前已存在的核心账户和参考数据表。

Revision ID: 20260719_0000
Revises:
Create Date: 2026-07-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260719_0000"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False
        ),
    )


def _create_account() -> None:
    op.create_table(
        "account",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("account_name", sa.String(128), nullable=False),
        sa.Column("account_type", sa.String(32), nullable=False),
        sa.Column("initial_cash", sa.Numeric(24, 6), nullable=False),
        sa.Column("cash_balance", sa.Numeric(24, 6), nullable=False),
        sa.Column("available_cash", sa.Numeric(24, 6), nullable=False),
        sa.Column("frozen_cash", sa.Numeric(24, 6), nullable=False),
        sa.Column("equity", sa.Numeric(24, 6), nullable=False),
        sa.Column("used_margin", sa.Numeric(24, 6), nullable=False),
        sa.Column("frozen_margin", sa.Numeric(24, 6), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(24, 6), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(24, 6), nullable=False),
        sa.Column("daily_pnl", sa.Numeric(24, 6), nullable=False),
        sa.Column("used_commission", sa.Numeric(24, 6), nullable=False),
        sa.Column("frozen_commission", sa.Numeric(24, 6), nullable=False),
        sa.Column("risk_ratio", sa.Numeric(18, 8), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_account_account_id",
        "account",
        ["account_id"],
        unique=True,
    )
    op.create_index("ix_account_user_id", "account", ["user_id"])
    op.create_index(
        "ix_account_trading_day", "account", ["trading_day"]
    )


def _create_instrument() -> None:
    op.create_table(
        "instrument",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_book_id", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False),
        sa.Column("instrument_name", sa.String(128), nullable=True),
        sa.Column("product_id", sa.String(64), nullable=True),
        sa.Column("market_type", sa.String(32), nullable=False),
        sa.Column(
            "contract_multiplier", sa.Numeric(18, 6), nullable=False
        ),
        sa.Column("price_tick", sa.Numeric(18, 6), nullable=False),
        sa.Column("min_volume", sa.Integer(), nullable=False),
        sa.Column("max_volume", sa.Integer(), nullable=False),
        sa.Column("listed_date", sa.Date(), nullable=True),
        sa.Column("expire_date", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("data_source", sa.String(32), nullable=False),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), nullable=True
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "exchange_id",
            "symbol",
            name="uq_instrument_exchange_symbol",
        ),
    )
    op.create_index(
        "ix_instrument_order_book_id",
        "instrument",
        ["order_book_id"],
        unique=True,
    )
    for name in (
        "symbol",
        "exchange_id",
        "product_id",
        "expire_date",
        "is_active",
    ):
        op.create_index(f"ix_instrument_{name}", "instrument", [name])


def _create_margin_rule(table_name: str, *, daily: bool) -> None:
    columns = [
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_book_id", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("long_margin_rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("short_margin_rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("min_margin_rate", sa.Numeric(18, 8), nullable=True),
        sa.Column("data_source", sa.String(32), nullable=False),
    ]
    if daily:
        columns.append(
            sa.Column("sync_batch_id", sa.String(64), nullable=True)
        )
    columns.extend(
        [
            sa.Column(
                "synced_at", sa.DateTime(timezone=True), nullable=False
            ),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        ]
    )
    unique_columns = (
        ("exchange_id", "symbol", "trading_day")
        if daily
        else ("exchange_id", "symbol")
    )
    columns.append(
        sa.UniqueConstraint(
            *unique_columns,
            name=(
                "uq_margin_rule_daily_exchange_symbol_day"
                if daily
                else "uq_margin_rule_exchange_symbol"
            ),
        )
    )
    op.create_table(table_name, *columns)
    for name in (
        "order_book_id",
        "symbol",
        "exchange_id",
        "trading_day",
        *(("sync_batch_id",) if daily else ()),
    ):
        op.create_index(f"ix_{table_name}_{name}", table_name, [name])


def _create_fee_rule(table_name: str, *, daily: bool) -> None:
    columns = [
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_book_id", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("commission_type", sa.String(32), nullable=False),
        sa.Column("open_commission", sa.Numeric(24, 12), nullable=False),
        sa.Column("close_commission", sa.Numeric(24, 12), nullable=False),
        sa.Column(
            "close_today_commission",
            sa.Numeric(24, 12),
            nullable=False,
        ),
        sa.Column("discount_rate", sa.Numeric(24, 12), nullable=True),
        sa.Column("data_source", sa.String(32), nullable=False),
    ]
    if daily:
        columns.append(
            sa.Column("sync_batch_id", sa.String(64), nullable=True)
        )
    columns.extend(
        [
            sa.Column(
                "synced_at", sa.DateTime(timezone=True), nullable=False
            ),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
        ]
    )
    unique_columns = (
        ("exchange_id", "symbol", "trading_day")
        if daily
        else ("exchange_id", "symbol")
    )
    columns.append(
        sa.UniqueConstraint(
            *unique_columns,
            name=(
                "uq_fee_rule_daily_exchange_symbol_day"
                if daily
                else "uq_fee_rule_exchange_symbol"
            ),
        )
    )
    op.create_table(table_name, *columns)
    for name in (
        "order_book_id",
        "symbol",
        "exchange_id",
        "trading_day",
        *(("sync_batch_id",) if daily else ()),
    ):
        op.create_index(f"ix_{table_name}_{name}", table_name, [name])


def _create_reference_sync_log() -> None:
    op.create_table(
        "reference_sync_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sync_batch_id", sa.String(64), nullable=False),
        sa.Column("data_source", sa.String(32), nullable=False),
        sa.Column("sync_type", sa.String(32), nullable=False),
        sa.Column("target_trading_day", sa.Date(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "finished_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reference_sync_log_sync_batch_id",
        "reference_sync_log",
        ["sync_batch_id"],
        unique=True,
    )
    op.create_index(
        "ix_reference_sync_log_target_trading_day",
        "reference_sync_log",
        ["target_trading_day"],
    )
    op.create_index(
        "ix_reference_sync_log_status",
        "reference_sync_log",
        ["status"],
    )


def upgrade() -> None:
    """建立后续历史迁移依赖的全部核心表。"""

    _create_account()
    _create_instrument()
    _create_margin_rule("margin_rule", daily=False)
    _create_margin_rule("margin_rule_daily", daily=True)
    _create_fee_rule("fee_rule", daily=False)
    _create_fee_rule("fee_rule_daily", daily=True)
    _create_reference_sync_log()


def downgrade() -> None:
    """按外部依赖的逆序移除核心基线表。"""

    for table_name in (
        "reference_sync_log",
        "fee_rule_daily",
        "fee_rule",
        "margin_rule_daily",
        "margin_rule",
        "instrument",
        "account",
    ):
        op.drop_table(table_name)
