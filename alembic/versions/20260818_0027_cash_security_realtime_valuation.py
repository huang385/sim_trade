"""Persist cash-security market marks and daily valuation basis.

Revision ID: 20260818_0027
Revises: 20260818_0026
"""

import sqlalchemy as sa
from alembic import op


revision = "20260818_0027"
down_revision = "20260818_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("position", sa.Column("market_value", sa.Numeric(24, 6), nullable=False, server_default="0"))
    op.add_column("position", sa.Column("mark_price", sa.Numeric(24, 6), nullable=True))
    op.add_column("position", sa.Column("mark_time", sa.DateTime(timezone=True), nullable=True))
    op.add_column("position", sa.Column("mark_source_event_id", sa.String(128), nullable=True))
    op.add_column("position", sa.Column("daily_pnl_base_cost", sa.Numeric(24, 6), nullable=False, server_default="0"))
    # Existing cash positions must remain readable before their first valid
    # mark.  Their historical cost is the only safe initial daily basis.
    op.execute("UPDATE position SET daily_pnl_base_cost = position_cost WHERE instrument_type IN ('STOCK', 'CONVERTIBLE_BOND')")


def downgrade() -> None:
    # The preceding 0026 migration remains the intentional irreversible
    # boundary for cash-security execution facts.  Keeping this additive
    # migration reversible preserves the established downgrade contract.
    op.drop_column("position", "daily_pnl_base_cost")
    op.drop_column("position", "mark_source_event_id")
    op.drop_column("position", "mark_time")
    op.drop_column("position", "mark_price")
    op.drop_column("position", "market_value")
