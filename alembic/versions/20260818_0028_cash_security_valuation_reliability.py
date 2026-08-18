"""Make cash-security daily valuation basis explicit and auditable.

Revision ID: 20260818_0028
Revises: 20260818_0027
"""

import sqlalchemy as sa
from alembic import op


revision = "20260818_0028"
down_revision = "20260818_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("position", sa.Column("yesterday_pnl_base_cost", sa.Numeric(24, 6), nullable=False, server_default="0"))
    op.add_column("position", sa.Column("today_pnl_base_cost", sa.Numeric(24, 6), nullable=False, server_default="0"))
    op.add_column("position", sa.Column("daily_pnl_base_established", sa.Boolean(), nullable=False, server_default=sa.false()))
    # 0027 could not prove an intraday baseline for pre-existing positions.
    # Keep those rows explicitly unestablished until a verified EOD mark.
    op.execute("UPDATE position SET daily_pnl_base_established = false WHERE instrument_type IN ('STOCK', 'CONVERTIBLE_BOND')")
    op.create_check_constraint("ck_position_market_value_nonnegative", "position", "market_value >= 0")
    op.create_check_constraint("ck_position_daily_pnl_base_cost_nonnegative", "position", "daily_pnl_base_cost >= 0")
    op.create_check_constraint("ck_position_daily_pnl_bucket_cost_nonnegative", "position", "yesterday_pnl_base_cost >= 0 AND today_pnl_base_cost >= 0")
    op.create_check_constraint("ck_position_mark_price_positive", "position", "mark_price IS NULL OR mark_price > 0")
    op.create_check_constraint("ck_position_mark_fields_consistent", "position", "(mark_price IS NULL AND mark_time IS NULL AND mark_source_event_id IS NULL) OR (mark_price IS NOT NULL AND mark_time IS NOT NULL AND mark_source_event_id IS NOT NULL)")


def downgrade() -> None:
    op.drop_constraint("ck_position_mark_fields_consistent", "position", type_="check")
    op.drop_constraint("ck_position_mark_price_positive", "position", type_="check")
    op.drop_constraint("ck_position_daily_pnl_bucket_cost_nonnegative", "position", type_="check")
    op.drop_constraint("ck_position_daily_pnl_base_cost_nonnegative", "position", type_="check")
    op.drop_constraint("ck_position_market_value_nonnegative", "position", type_="check")
    op.drop_column("position", "daily_pnl_base_established")
    op.drop_column("position", "today_pnl_base_cost")
    op.drop_column("position", "yesterday_pnl_base_cost")
