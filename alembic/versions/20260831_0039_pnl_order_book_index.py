"""Add the realtime PnL incremental-refresh routing index.

Revision ID: 20260831_0039
Revises: 20260824_0038
"""

from alembic import op


revision = "20260831_0039"
down_revision = "20260824_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_position_exchange_order_book_id",
        "position",
        ["exchange_id", "order_book_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_position_exchange_order_book_id", table_name="position")
