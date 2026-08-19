"""Add corporate-action business dates and safely backfill replay facts.

Revision ID: 20260819_0035
Revises: 20260819_0034
"""

import sqlalchemy as sa
from alembic import op


revision = "20260819_0035"
down_revision = "20260819_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cash_security_corporate_action_ledger",
        sa.Column("effective_trading_day", sa.Date(), nullable=True),
    )
    op.create_index(
        "ix_cash_corporate_ledger_effective_day",
        "cash_security_corporate_action_ledger",
        ["effective_trading_day"],
    )
    # A ledger's write timestamp is not a business date.  These event types
    # have an unambiguous day in the action definition.  The residual rows are
    # deliberately flagged for manual review below instead of being silently
    # treated as verified history.
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
        """
    )
    # All supported action rows have an ex-date.  This defensive fallback is
    # only retained for malformed legacy rows, which are marked below.
    op.execute(
        "UPDATE cash_security_corporate_action_ledger "
        "SET effective_trading_day = CURRENT_DATE "
        "WHERE effective_trading_day IS NULL"
    )
    op.alter_column(
        "cash_security_corporate_action_ledger",
        "effective_trading_day",
        nullable=False,
    )

    # The following three entry types contain enough immutable information to
    # rebuild their adjustment without guessing.  The idempotency key is
    # deterministic, so a restored/partially-run migration cannot duplicate a
    # fact.  Splits and maturity retirements require pre-action bucket data
    # that old ledgers did not retain; they are explicitly sent to review.
    op.execute(
        """
        INSERT INTO cash_security_corporate_action_position_adjustment (
            adjustment_id, action_id, action_version, component_id,
            entitlement_id, account_id, position_id, position_detail_id,
            adjustment_type, effective_trading_day, business_version,
            idempotency_key, total_volume_delta, today_volume_delta,
            yesterday_volume_delta, pending_volume_delta,
            available_volume_delta, frozen_volume_delta,
            settlement_locked_volume_delta, position_cost_delta,
            daily_pnl_base_cost_delta, average_open_price_after,
            replay_payload, created_at
        )
        SELECT
            'CAPA-BACKFILL-' || ledger.ledger_id,
            ledger.action_id,
            action.action_version,
            ledger.component_id,
            ledger.entitlement_id,
            ledger.account_id,
            ledger.position_id,
            NULL,
            ledger.entry_type,
            ledger.effective_trading_day,
            CAST(action.action_version AS VARCHAR),
            ledger.idempotency_key || '-POSITION_ADJUSTMENT',
            CASE WHEN ledger.entry_type = 'SHARES_LISTED' THEN ledger.position_volume_delta ELSE 0 END,
            0,
            CASE WHEN ledger.entry_type = 'SHARES_LISTED' THEN ledger.position_volume_delta ELSE 0 END,
            ledger.pending_volume_delta,
            CASE WHEN ledger.entry_type = 'SHARES_LISTED' THEN ledger.position_volume_delta ELSE 0 END,
            0,
            0,
            CASE
                WHEN ledger.entry_type = 'SHARES_LISTED' THEN COALESCE(entitlement.subscription_cash, ledger.position_cost_delta)
                ELSE ledger.position_cost_delta
            END,
            0,
            NULL,
            '{"backfill_source":"ledger"}',
            ledger.created_at
        FROM cash_security_corporate_action_ledger AS ledger
        JOIN cash_security_corporate_action AS action ON action.action_id = ledger.action_id
        LEFT JOIN cash_security_corporate_action_entitlement AS entitlement
          ON entitlement.entitlement_id = ledger.entitlement_id
        WHERE ledger.entry_type IN ('SHARES_PENDING', 'SHARES_LISTED', 'RIGHTS_SUBSCRIBED')
          AND NOT EXISTS (
              SELECT 1
              FROM cash_security_corporate_action_position_adjustment AS existing
              WHERE existing.idempotency_key = ledger.idempotency_key || '-POSITION_ADJUSTMENT'
          )
        """
    )
    # Existing split/maturity effects lack sufficient pre-event bucket and
    # cost data.  Mark the source action rather than invent a false replay
    # fact.  Operators can then append an explicit opening-balance/reversal.
    op.execute(
        """
        UPDATE cash_security_corporate_action AS action
        SET status = 'MANUAL_REVIEW_REQUIRED'
        WHERE EXISTS (
            SELECT 1 FROM cash_security_corporate_action_ledger AS ledger
            WHERE ledger.action_id = action.action_id
              AND ledger.entry_type IN (
                'STOCK_SPLIT', 'REVERSE_SPLIT', 'BOND_PRINCIPAL_RECEIVABLE'
              )
        )
        """
    )


def downgrade() -> None:
    raise RuntimeError("Corporate-action replay facts and business dates are irreversible")
