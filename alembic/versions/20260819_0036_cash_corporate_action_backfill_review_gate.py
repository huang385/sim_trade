"""Gate ambiguous historical corporate-action replay rows for review.

Revision ID: 20260819_0036
Revises: 20260819_0035
"""

from alembic import op


revision = "20260819_0036"
down_revision = "20260819_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A historical action applied to an imported opening position with no cash
    # Trade history has no provable pre-action baseline.  Keep its ledger and
    # backfilled adjustment rows for audit, but make the action ineligible for
    # automatic replay until an operator appends a verified opening fact.
    op.execute(
        """
        UPDATE cash_security_corporate_action AS action
        SET status = 'MANUAL_REVIEW_REQUIRED'
        WHERE EXISTS (
            SELECT 1
            FROM cash_security_corporate_action_ledger AS ledger
            JOIN position AS holding ON holding.position_id = ledger.position_id
            WHERE ledger.action_id = action.action_id
              AND ledger.entry_type IN (
                'SHARES_PENDING', 'SHARES_LISTED', 'RIGHTS_SUBSCRIBED',
                'STOCK_SPLIT', 'REVERSE_SPLIT', 'BOND_PRINCIPAL_RECEIVABLE'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM trade
                  WHERE trade.account_id = holding.account_id
                    AND trade.exchange_id = holding.exchange_id
                    AND trade.symbol = holding.symbol
                    AND trade.instrument_type = holding.instrument_type
              )
        )
        """
    )


def downgrade() -> None:
    raise RuntimeError("Manual review gates for ambiguous history are irreversible")
