"""Backfill the legacy single-request rights-subscription audit trail.

Revision ID: 20260819_0033
Revises: 20260819_0032
"""

from alembic import op


revision = "20260819_0033"
down_revision = "20260819_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Before 0032 an entitlement could carry only one request id.  Preserve
    # the aggregate accepted request so retries remain idempotent after the
    # system starts allowing additional partial subscriptions.
    op.execute(
        """
        INSERT INTO cash_security_corporate_action_subscription
            (subscription_id, entitlement_id, action_id, account_id,
             client_request_id, volume, cash_amount, created_at)
        SELECT
            'CAS-LEGACY-' || substr(md5(entitlement_id || ':' || client_request_id), 1, 32),
            entitlement_id, action_id, account_id, client_request_id,
            subscribed_volume, subscription_cash,
            COALESCE(updated_at, created_at)
        FROM cash_security_corporate_action_entitlement
        WHERE client_request_id IS NOT NULL AND subscribed_volume > 0
        ON CONFLICT (entitlement_id, client_request_id) DO NOTHING
        """
    )


def downgrade() -> None:
    raise RuntimeError("Legacy rights-subscription audit backfill is intentionally irreversible")
