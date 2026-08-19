"""Persist replayable cash-security corporate-action position facts.

Revision ID: 20260819_0034
Revises: 20260819_0033
"""

import sqlalchemy as sa
from alembic import op


revision = "20260819_0034"
down_revision = "20260819_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cash_security_corporate_action_position_adjustment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("adjustment_id", sa.String(64), nullable=False),
        sa.Column("action_id", sa.String(64), sa.ForeignKey("cash_security_corporate_action.action_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("action_version", sa.Integer(), nullable=False),
        sa.Column("component_id", sa.String(64), sa.ForeignKey("cash_security_corporate_action_component.component_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("entitlement_id", sa.String(64), sa.ForeignKey("cash_security_corporate_action_entitlement.entitlement_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("account_id", sa.String(64), sa.ForeignKey("account.account_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("position_id", sa.String(64), sa.ForeignKey("position.position_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("position_detail_id", sa.String(64), nullable=True),
        sa.Column("adjustment_type", sa.String(48), nullable=False),
        sa.Column("effective_trading_day", sa.Date(), nullable=False),
        sa.Column("business_version", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(192), nullable=False),
        sa.Column("total_volume_delta", sa.Integer(), nullable=False),
        sa.Column("today_volume_delta", sa.Integer(), nullable=False),
        sa.Column("yesterday_volume_delta", sa.Integer(), nullable=False),
        sa.Column("pending_volume_delta", sa.Integer(), nullable=False),
        sa.Column("available_volume_delta", sa.Integer(), nullable=False),
        sa.Column("frozen_volume_delta", sa.Integer(), nullable=False),
        sa.Column("settlement_locked_volume_delta", sa.Integer(), nullable=False),
        sa.Column("position_cost_delta", sa.Numeric(24, 6), nullable=False),
        sa.Column("daily_pnl_base_cost_delta", sa.Numeric(24, 6), nullable=False),
        sa.Column("average_open_price_after", sa.Numeric(24, 6), nullable=True),
        sa.Column("replay_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("adjustment_id", name="uq_cash_corporate_adjustment_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_cash_corporate_adjustment_idempotency"),
    )
    op.create_index("ix_cash_corporate_adjustment_position_day", "cash_security_corporate_action_position_adjustment", ["position_id", "effective_trading_day"])
    op.create_index("ix_cash_corporate_adjustment_detail_day", "cash_security_corporate_action_position_adjustment", ["position_detail_id", "effective_trading_day"])
    op.create_index("ix_cash_corporate_adjustment_action_component", "cash_security_corporate_action_position_adjustment", ["action_id", "component_id"])
    op.create_index("ix_cash_corporate_adjustment_account_day", "cash_security_corporate_action_position_adjustment", ["account_id", "effective_trading_day"])


def downgrade() -> None:
    raise RuntimeError("Corporate-action position facts are append-only and irreversible")
