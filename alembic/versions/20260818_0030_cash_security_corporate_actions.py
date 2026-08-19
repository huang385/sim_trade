"""Add auditable cash-security corporate action facts.

Revision ID: 20260818_0030
Revises: 20260818_0029
"""

import sqlalchemy as sa
from alembic import op


revision = "20260818_0030"
down_revision = "20260818_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("account", sa.Column("corporate_action_receivable", sa.Numeric(24, 6), nullable=False, server_default="0"))
    op.add_column("account", sa.Column("corporate_action_income", sa.Numeric(24, 6), nullable=False, server_default="0"))
    op.add_column("account", sa.Column("pending_security_value", sa.Numeric(24, 6), nullable=False, server_default="0"))
    op.add_column("account", sa.Column("rights_subscription_receivable", sa.Numeric(24, 6), nullable=False, server_default="0"))
    op.add_column("position", sa.Column("pending_share_volume", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint("ck_account_corporate_receivable_nonnegative", "account", "corporate_action_receivable >= 0")
    op.create_check_constraint("ck_account_pending_security_value_nonnegative", "account", "pending_security_value >= 0")
    op.create_check_constraint("ck_position_pending_share_nonnegative", "position", "pending_share_volume >= 0")
    op.create_table("cash_security_corporate_action",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("action_id", sa.String(64), nullable=False),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instrument.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False), sa.Column("order_book_id", sa.String(64), nullable=False),
        sa.Column("action_version", sa.Integer(), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("announcement_date", sa.Date()), sa.Column("record_date", sa.Date()), sa.Column("ex_date", sa.Date()),
        sa.Column("payment_date", sa.Date()), sa.Column("listing_date", sa.Date()), sa.Column("subscription_start_date", sa.Date()), sa.Column("subscription_end_date", sa.Date()),
        sa.Column("source_action_id", sa.String(128), nullable=False), sa.Column("data_source", sa.String(32), nullable=False),
        sa.Column("source_payload_hash", sa.String(128), nullable=False), sa.Column("source_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)), sa.Column("completed_at", sa.DateTime(timezone=True)), sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("action_id", name="uq_cash_corporate_action_id"), sa.UniqueConstraint("source_action_id", "action_version", name="uq_cash_corporate_action_source_version"),
    )
    op.create_index("ix_cash_corporate_action_record", "cash_security_corporate_action", ["record_date"])
    op.create_table("cash_security_corporate_action_component",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("component_id", sa.String(64), nullable=False),
        sa.Column("action_id", sa.String(64), sa.ForeignKey("cash_security_corporate_action.action_id", ondelete="RESTRICT"), nullable=False), sa.Column("component_type", sa.String(32), nullable=False),
        sa.Column("base_quantity", sa.Numeric(24, 6), nullable=False), sa.Column("cash_amount", sa.Numeric(24, 6), nullable=False, server_default="0"), sa.Column("share_ratio", sa.Numeric(24, 12), nullable=False, server_default="0"), sa.Column("rights_ratio", sa.Numeric(24, 12), nullable=False, server_default="0"), sa.Column("subscription_price", sa.Numeric(24, 6), nullable=False, server_default="0"), sa.Column("withholding_tax_rate", sa.Numeric(18, 12), nullable=False, server_default="0"), sa.Column("cash_in_lieu_price", sa.Numeric(24, 6), nullable=False, server_default="0"), sa.Column("rounding_rule", sa.String(32), nullable=False, server_default="FLOOR"), sa.Column("currency", sa.String(8), nullable=False, server_default="CNY"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("component_id", name="uq_cash_corporate_component_id"), sa.CheckConstraint("base_quantity > 0", name="ck_cash_corporate_component_base_positive"), sa.CheckConstraint("cash_amount >= 0 AND share_ratio >= 0 AND rights_ratio >= 0 AND subscription_price >= 0 AND withholding_tax_rate >= 0 AND withholding_tax_rate <= 1 AND cash_in_lieu_price >= 0", name="ck_cash_corporate_component_amounts_valid"),
    )
    op.create_table("cash_security_corporate_action_entitlement",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("entitlement_id", sa.String(64), nullable=False, unique=True), sa.Column("action_id", sa.String(64), sa.ForeignKey("cash_security_corporate_action.action_id", ondelete="RESTRICT"), nullable=False), sa.Column("component_id", sa.String(64), sa.ForeignKey("cash_security_corporate_action_component.component_id", ondelete="RESTRICT"), nullable=False), sa.Column("account_id", sa.String(64), sa.ForeignKey("account.account_id", ondelete="RESTRICT"), nullable=False), sa.Column("position_id", sa.String(64), sa.ForeignKey("position.position_id", ondelete="RESTRICT"), nullable=False), sa.Column("record_quantity", sa.Integer(), nullable=False), sa.Column("entitled_cash_gross", sa.Numeric(24, 6), nullable=False, server_default="0"), sa.Column("withholding_tax", sa.Numeric(24, 6), nullable=False, server_default="0"), sa.Column("entitled_cash_net", sa.Numeric(24, 6), nullable=False, server_default="0"), sa.Column("entitled_share_volume", sa.Integer(), nullable=False, server_default="0"), sa.Column("fractional_share", sa.Numeric(24, 12), nullable=False, server_default="0"), sa.Column("cash_in_lieu", sa.Numeric(24, 6), nullable=False, server_default="0"), sa.Column("subscribed_volume", sa.Integer(), nullable=False, server_default="0"), sa.Column("subscription_cash", sa.Numeric(24, 6), nullable=False, server_default="0"), sa.Column("pending_share_volume", sa.Integer(), nullable=False, server_default="0"), sa.Column("credited_share_volume", sa.Integer(), nullable=False, server_default="0"), sa.Column("status", sa.String(32), nullable=False), sa.Column("record_position_version", sa.String(128), nullable=False), sa.Column("client_request_id", sa.String(128)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("processed_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("action_id", "component_id", "account_id", "position_id", name="uq_cash_corporate_entitlement"), sa.CheckConstraint("record_quantity >= 0 AND entitled_cash_gross >= 0 AND withholding_tax >= 0 AND entitled_cash_net >= 0 AND entitled_share_volume >= 0 AND fractional_share >= 0 AND cash_in_lieu >= 0 AND subscribed_volume >= 0 AND subscription_cash >= 0 AND pending_share_volume >= 0 AND credited_share_volume >= 0", name="ck_cash_corporate_entitlement_nonnegative"),
    )
    op.create_table("cash_security_corporate_action_ledger",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("ledger_id", sa.String(64), nullable=False, unique=True), sa.Column("action_id", sa.String(64), sa.ForeignKey("cash_security_corporate_action.action_id", ondelete="RESTRICT"), nullable=False), sa.Column("component_id", sa.String(64), sa.ForeignKey("cash_security_corporate_action_component.component_id", ondelete="RESTRICT"), nullable=False), sa.Column("entitlement_id", sa.String(64), sa.ForeignKey("cash_security_corporate_action_entitlement.entitlement_id", ondelete="RESTRICT"), nullable=False), sa.Column("account_id", sa.String(64), sa.ForeignKey("account.account_id", ondelete="RESTRICT"), nullable=False), sa.Column("position_id", sa.String(64), sa.ForeignKey("position.position_id", ondelete="RESTRICT")), sa.Column("entry_type", sa.String(48), nullable=False), sa.Column("cash_delta", sa.Numeric(24, 6), nullable=False, server_default="0"), sa.Column("receivable_delta", sa.Numeric(24, 6), nullable=False, server_default="0"), sa.Column("position_volume_delta", sa.Integer(), nullable=False, server_default="0"), sa.Column("pending_volume_delta", sa.Integer(), nullable=False, server_default="0"), sa.Column("position_cost_delta", sa.Numeric(24, 6), nullable=False, server_default="0"), sa.Column("corporate_action_income_delta", sa.Numeric(24, 6), nullable=False, server_default="0"), sa.Column("business_version", sa.String(128), nullable=False), sa.Column("idempotency_key", sa.String(192), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("idempotency_key", name="uq_cash_corporate_ledger_idempotency"),
    )
    op.create_table("cash_security_price_adjustment_factor",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instrument.id", ondelete="RESTRICT"), nullable=False), sa.Column("trading_day", sa.Date(), nullable=False), sa.Column("action_id", sa.String(64), sa.ForeignKey("cash_security_corporate_action.action_id", ondelete="RESTRICT"), nullable=False), sa.Column("raw_previous_close", sa.Numeric(24, 6), nullable=False), sa.Column("official_ex_reference_price", sa.Numeric(24, 6), nullable=False), sa.Column("forward_adjustment_factor", sa.Numeric(24, 12), nullable=False), sa.Column("backward_adjustment_factor", sa.Numeric(24, 12), nullable=False), sa.Column("source_event_id", sa.String(128), nullable=False), sa.Column("data_source", sa.String(32), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("instrument_id", "trading_day", "action_id", name="uq_cash_price_adjustment_factor"), sa.CheckConstraint("raw_previous_close > 0 AND official_ex_reference_price > 0 AND forward_adjustment_factor > 0 AND backward_adjustment_factor > 0", name="ck_cash_price_adjustment_factor_positive"),
    )


def downgrade() -> None:
    raise RuntimeError("0030 contains auditable corporate-action facts and is intentionally irreversible")
