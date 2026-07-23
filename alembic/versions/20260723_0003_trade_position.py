"""创建成交、持仓汇总和逐笔持仓明细表

Revision ID: 20260723_0003
Revises: 20260720_0002
Create Date: 2026-07-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260723_0003"
down_revision: Union[str, None] = "20260720_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """建立成交和持仓结构，所有金额字段统一使用 Numeric(24, 6)。"""

    op.create_table(
        "trade",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trade_id", sa.String(64), nullable=False),
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("market_event_id", sa.String(128), nullable=False),
        sa.Column("market_stream_message_id", sa.String(64), nullable=False),
        sa.Column("order_book_id", sa.String(64), nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("offset_flag", sa.String(32), nullable=False),
        sa.Column("trade_price", sa.Numeric(24, 6), nullable=False),
        sa.Column("trade_volume", sa.Integer(), nullable=False),
        sa.Column("turnover", sa.Numeric(24, 6), nullable=False),
        sa.Column("margin", sa.Numeric(24, 6), nullable=False),
        sa.Column("commission", sa.Numeric(24, 6), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(24, 6), nullable=False),
        sa.Column("trade_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("trade_price > 0", name="ck_trade_price_positive"),
        sa.CheckConstraint("trade_volume > 0", name="ck_trade_volume_positive"),
        sa.CheckConstraint("turnover >= 0", name="ck_trade_turnover_nonnegative"),
        sa.CheckConstraint("margin >= 0", name="ck_trade_margin_nonnegative"),
        sa.CheckConstraint(
            "commission >= 0", name="ck_trade_commission_nonnegative"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trade"),
        sa.UniqueConstraint("trade_id", name="uq_trade_trade_id"),
        sa.UniqueConstraint(
            "order_id", "market_event_id", name="uq_trade_order_market_event"
        ),
    )
    op.create_index("ix_trade_order_id", "trade", ["order_id"])
    op.create_index("ix_trade_account_id", "trade", ["account_id"])
    op.create_index(
        "ix_trade_exchange_symbol", "trade", ["exchange_id", "symbol"]
    )
    op.create_index("ix_trade_trading_day", "trade", ["trading_day"])
    op.create_index("ix_trade_trade_time", "trade", ["trade_time"])

    op.create_table(
        "position",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("position_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("order_book_id", sa.String(64), nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("total_volume", sa.Integer(), nullable=False),
        sa.Column("today_volume", sa.Integer(), nullable=False),
        sa.Column("yesterday_volume", sa.Integer(), nullable=False),
        sa.Column("frozen_volume", sa.Integer(), nullable=False),
        sa.Column("available_volume", sa.Integer(), nullable=False),
        sa.Column("average_open_price", sa.Numeric(24, 6), nullable=False),
        sa.Column("position_cost", sa.Numeric(24, 6), nullable=False),
        sa.Column("used_margin", sa.Numeric(24, 6), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(24, 6), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(24, 6), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("total_volume >= 0", name="ck_position_total_nonnegative"),
        sa.CheckConstraint("today_volume >= 0", name="ck_position_today_nonnegative"),
        sa.CheckConstraint(
            "yesterday_volume >= 0", name="ck_position_yesterday_nonnegative"
        ),
        sa.CheckConstraint("frozen_volume >= 0", name="ck_position_frozen_nonnegative"),
        sa.CheckConstraint(
            "available_volume >= 0", name="ck_position_available_nonnegative"
        ),
        sa.CheckConstraint(
            "total_volume = today_volume + yesterday_volume",
            name="ck_position_day_volume_balance",
        ),
        sa.CheckConstraint(
            "available_volume = total_volume - frozen_volume",
            name="ck_position_available_balance",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_position"),
        sa.UniqueConstraint("position_id", name="uq_position_position_id"),
        sa.UniqueConstraint(
            "account_id",
            "exchange_id",
            "symbol",
            "direction",
            name="uq_position_account_contract_direction",
        ),
    )
    op.create_index("ix_position_account_id", "position", ["account_id"])
    op.create_index(
        "ix_position_exchange_symbol", "position", ["exchange_id", "symbol"]
    )
    op.create_index("ix_position_trading_day", "position", ["trading_day"])

    op.create_table(
        "position_detail",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("position_detail_id", sa.String(64), nullable=False),
        sa.Column("position_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("open_trade_id", sa.String(64), nullable=False),
        sa.Column("order_book_id", sa.String(64), nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("open_trading_day", sa.Date(), nullable=False),
        sa.Column("open_price", sa.Numeric(24, 6), nullable=False),
        sa.Column("original_volume", sa.Integer(), nullable=False),
        sa.Column("remaining_volume", sa.Integer(), nullable=False),
        sa.Column("frozen_volume", sa.Integer(), nullable=False),
        sa.Column("open_margin", sa.Numeric(24, 6), nullable=False),
        sa.Column("open_commission", sa.Numeric(24, 6), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "original_volume > 0", name="ck_position_detail_original_positive"
        ),
        sa.CheckConstraint(
            "remaining_volume >= 0", name="ck_position_detail_remaining_nonnegative"
        ),
        sa.CheckConstraint(
            "frozen_volume >= 0", name="ck_position_detail_frozen_nonnegative"
        ),
        sa.CheckConstraint(
            "remaining_volume <= original_volume",
            name="ck_position_detail_remaining_limit",
        ),
        sa.CheckConstraint(
            "frozen_volume <= remaining_volume",
            name="ck_position_detail_frozen_limit",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_position_detail"),
        sa.UniqueConstraint(
            "position_detail_id", name="uq_position_detail_detail_id"
        ),
        sa.UniqueConstraint("open_trade_id", name="uq_position_detail_open_trade"),
    )
    op.create_index(
        "ix_position_detail_position_id", "position_detail", ["position_id"]
    )
    op.create_index(
        "ix_position_detail_account_id", "position_detail", ["account_id"]
    )
    op.create_index(
        "ix_position_detail_open_trading_day",
        "position_detail",
        ["open_trading_day"],
    )


def downgrade() -> None:
    """按依赖方向倒序删除本阶段三张表。"""

    op.drop_table("position_detail")
    op.drop_table("position")
    op.drop_table("trade")
