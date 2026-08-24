"""Repair unsafe legacy corporate-action dates and gate replay.

Revision ID: 20260824_0038
Revises: 20260820_0037
"""

from alembic import op


revision = "20260824_0038"
down_revision = "20260820_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Re-derive all dates that have an authoritative action-date source.  This
    # intentionally overwrites the 0035 CURRENT_DATE fallback for known rows.
    op.execute(
        """
        UPDATE cash_security_corporate_action_ledger AS ledger
        SET effective_trading_day = CASE
            WHEN ledger.entry_type = 'SHARES_LISTED' THEN action.listing_date
            WHEN ledger.entry_type = 'CASH_PAID' THEN COALESCE(action.payment_date, action.ex_date)
            ELSE action.ex_date
        END
        FROM cash_security_corporate_action AS action
        WHERE action.action_id = ledger.action_id
          AND (
              (ledger.entry_type = 'SHARES_LISTED' AND action.listing_date IS NOT NULL)
              OR (ledger.entry_type = 'CASH_PAID' AND COALESCE(action.payment_date, action.ex_date) IS NOT NULL)
              OR (ledger.entry_type NOT IN ('SHARES_LISTED', 'CASH_PAID') AND action.ex_date IS NOT NULL)
          )
        """
    )
    # No CURRENT_DATE fallback is permitted.  If a legacy entry has no
    # authoritative business day, retain its immutable row for audit but make
    # the whole action ineligible for automatic cash or position replay.
    op.execute(
        """
        UPDATE cash_security_corporate_action AS action
        SET status = 'MANUAL_REVIEW_REQUIRED'
        WHERE EXISTS (
            SELECT 1
            FROM cash_security_corporate_action_ledger AS ledger
            WHERE ledger.action_id = action.action_id
              AND (
                  (ledger.entry_type = 'SHARES_LISTED' AND action.listing_date IS NULL)
                  OR (ledger.entry_type = 'CASH_PAID' AND action.payment_date IS NULL AND action.ex_date IS NULL)
                  OR (ledger.entry_type NOT IN ('SHARES_LISTED', 'CASH_PAID') AND action.ex_date IS NULL)
              )
        )
        """
    )


def downgrade() -> None:
    raise RuntimeError("Fail-closed legacy corporate-action repair is irreversible")
