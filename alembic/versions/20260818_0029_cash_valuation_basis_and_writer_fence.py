"""Repair historical cash basis buckets and add a durable writer fence.

Revision ID: 20260818_0029
Revises: 20260818_0028
"""

import sqlalchemy as sa
from alembic import op


revision = "20260818_0029"
down_revision = "20260818_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cash_security_pnl_basis_migration_audit",
        sa.Column("position_id", sa.String(64), primary_key=True),
        sa.Column("previous_daily_pnl_base_cost", sa.Numeric(24, 6), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    # Only all-yesterday positions have a provable allocation.  Mixed or
    # today-only historical positions remain explicitly unestablished.
    op.execute("""
        INSERT INTO cash_security_pnl_basis_migration_audit
            (position_id, previous_daily_pnl_base_cost, action)
        SELECT position_id, daily_pnl_base_cost, 'BACKFILLED_YESTERDAY_BUCKET'
        FROM position
        WHERE instrument_type IN ('STOCK', 'CONVERTIBLE_BOND')
          AND daily_pnl_base_established = false
          AND total_volume > 0
          AND today_volume = 0
          AND yesterday_volume = total_volume
    """)
    op.execute("""
        UPDATE position
        SET yesterday_pnl_base_cost = daily_pnl_base_cost,
            today_pnl_base_cost = 0,
            daily_pnl_base_established = true
        WHERE instrument_type IN ('STOCK', 'CONVERTIBLE_BOND')
          AND daily_pnl_base_established = false
          AND total_volume > 0
          AND today_volume = 0
          AND yesterday_volume = total_volume
    """)
    op.create_table(
        "cash_security_valuation_writer_fence",
        sa.Column("fence_name", sa.String(64), primary_key=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("owner", sa.String(256), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("cash_security_valuation_writer_fence")
    op.drop_table("cash_security_pnl_basis_migration_audit")
