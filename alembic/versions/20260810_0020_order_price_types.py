"""Add auditable order price types and acceptance snapshots.

Revision ID: 20260810_0020
Revises: 20260806_0019
"""

from alembic import op
import sqlalchemy as sa


revision = "20260810_0020"
down_revision = "20260806_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = (
        sa.Column("submitted_limit_price", sa.Numeric(24, 6), nullable=True),
        sa.Column("resolved_price", sa.Numeric(24, 6), nullable=True),
        sa.Column("market_protection_price", sa.Numeric(24, 6), nullable=True),
        sa.Column("price_snapshot_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("price_snapshot_source", sa.String(64), nullable=True),
        sa.Column("price_snapshot_event_id", sa.String(128), nullable=True),
        sa.Column("price_snapshot_stream_message_id", sa.String(64), nullable=True),
        sa.Column("price_snapshot_bid1", sa.Numeric(24, 6), nullable=True),
        sa.Column("price_snapshot_bid_volume1", sa.Integer(), nullable=True),
        sa.Column("price_snapshot_ask1", sa.Numeric(24, 6), nullable=True),
        sa.Column("price_snapshot_ask_volume1", sa.Integer(), nullable=True),
        sa.Column("price_snapshot_last", sa.Numeric(24, 6), nullable=True),
        sa.Column("cancel_reason_code", sa.String(64), nullable=True),
        sa.Column("cancel_reason_message", sa.String(256), nullable=True),
    )
    for column in columns:
        op.add_column("orders", column)
    op.execute(
        "UPDATE orders SET submitted_limit_price = limit_price, "
        "resolved_price = limit_price WHERE order_type = 'LIMIT'"
    )
    op.alter_column("orders", "resolved_price", nullable=False)
    op.create_check_constraint(
        "ck_order_price_type_valid",
        "orders",
        "order_type IN ('LIMIT', 'COUNTERPARTY', 'LAST', 'MARKET')",
    )
    op.create_check_constraint(
        "ck_order_requested_price_consistent",
        "orders",
        "(order_type = 'LIMIT' AND submitted_limit_price IS NOT NULL) OR "
        "(order_type <> 'LIMIT' AND submitted_limit_price IS NULL)",
    )
    op.create_check_constraint(
        "ck_order_resolved_price_positive",
        "orders",
        "resolved_price > 0 AND limit_price = resolved_price",
    )
    op.create_check_constraint(
        "ck_order_market_protection_consistent",
        "orders",
        "(order_type = 'MARKET' AND market_protection_price IS NOT NULL) OR "
        "(order_type <> 'MARKET' AND market_protection_price IS NULL)",
    )


def downgrade() -> None:
    for name in (
        "ck_order_market_protection_consistent",
        "ck_order_resolved_price_positive",
        "ck_order_requested_price_consistent",
        "ck_order_price_type_valid",
    ):
        op.drop_constraint(name, "orders", type_="check")
    for name in (
        "cancel_reason_message",
        "cancel_reason_code",
        "price_snapshot_last",
        "price_snapshot_ask_volume1",
        "price_snapshot_ask1",
        "price_snapshot_bid_volume1",
        "price_snapshot_bid1",
        "price_snapshot_stream_message_id",
        "price_snapshot_event_id",
        "price_snapshot_source",
        "price_snapshot_time",
        "market_protection_price",
        "resolved_price",
        "submitted_limit_price",
    ):
        op.drop_column("orders", name)
