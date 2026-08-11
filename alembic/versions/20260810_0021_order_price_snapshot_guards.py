"""Enforce audit completeness for snapshot-priced orders.

Revision ID: 20260810_0021
Revises: 20260810_0020
"""

from alembic import op


revision = "20260810_0021"
down_revision = "20260810_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_order_price_snapshot_complete",
        "orders",
        "order_type = 'LIMIT' OR ("
        "price_snapshot_time IS NOT NULL AND "
        "price_snapshot_source IS NOT NULL AND "
        "price_snapshot_event_id IS NOT NULL AND "
        "price_snapshot_stream_message_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_order_price_snapshot_quote_valid",
        "orders",
        "order_type = 'LIMIT' OR "
        "(order_type = 'LAST' AND price_snapshot_last > 0) OR "
        "(order_type IN ('COUNTERPARTY', 'MARKET') AND ("
        "(direction = 'BUY' AND price_snapshot_ask1 > 0 "
        "AND price_snapshot_ask_volume1 > 0) OR "
        "(direction = 'SELL' AND price_snapshot_bid1 > 0 "
        "AND price_snapshot_bid_volume1 > 0)))",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_order_price_snapshot_quote_valid", "orders", type_="check"
    )
    op.drop_constraint(
        "ck_order_price_snapshot_complete", "orders", type_="check"
    )
