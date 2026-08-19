"""Align corporate-action lookup indexes with ORM metadata.

Revision ID: 20260818_0031
Revises: 20260818_0030
"""

from alembic import op


revision = "20260818_0031"
down_revision = "20260818_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_cash_corporate_action_record", table_name="cash_security_corporate_action")
    op.create_index("ix_cash_security_corporate_action_instrument_id", "cash_security_corporate_action", ["instrument_id"])
    op.create_index("ix_cash_security_corporate_action_status", "cash_security_corporate_action", ["status"])
    op.create_index("ix_cash_security_corporate_action_record_date", "cash_security_corporate_action", ["record_date"])
    op.create_index("ix_cash_security_corporate_action_ex_date", "cash_security_corporate_action", ["ex_date"])
    op.create_index("ix_cash_security_corporate_action_component_action_id", "cash_security_corporate_action_component", ["action_id"])
    op.create_index("ix_cash_security_corporate_action_entitlement_action_id", "cash_security_corporate_action_entitlement", ["action_id"])
    op.create_index("ix_cash_security_corporate_action_entitlement_component_id", "cash_security_corporate_action_entitlement", ["component_id"])
    op.create_index("ix_cash_security_corporate_action_entitlement_account_id", "cash_security_corporate_action_entitlement", ["account_id"])
    op.create_index("ix_cash_security_corporate_action_ledger_action_id", "cash_security_corporate_action_ledger", ["action_id"])
    op.create_index("ix_cash_security_corporate_action_ledger_account_id", "cash_security_corporate_action_ledger", ["account_id"])
    op.create_index("ix_cash_security_price_adjustment_factor_instrument_id", "cash_security_price_adjustment_factor", ["instrument_id"])
    op.create_index("ix_cash_security_price_adjustment_factor_trading_day", "cash_security_price_adjustment_factor", ["trading_day"])


def downgrade() -> None:
    raise RuntimeError("Corporate-action facts and their audit indexes are intentionally irreversible")
