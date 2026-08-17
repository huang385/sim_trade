"""Freeze current and previous trading-day last Tick prices for settlement.

Revision ID: 20260814_0022
Revises: 20260810_0021
"""

import sqlalchemy as sa
from alembic import op


revision = "20260814_0022"
down_revision = "20260810_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instrument_settlement_price",
        sa.Column("previous_last_price", sa.Numeric(24, 6), nullable=True),
    )
    op.add_column(
        "instrument_settlement_price",
        sa.Column(
            "previous_source_tick_time",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "instrument_settlement_price",
        sa.Column("previous_source_tick_trading_day", sa.Date(), nullable=True),
    )
    op.add_column(
        "instrument_settlement_price",
        sa.Column("previous_source_event_id", sa.String(128), nullable=True),
    )
    op.create_check_constraint(
        "ck_settlement_previous_last_price_positive",
        "instrument_settlement_price",
        "previous_last_price IS NULL OR previous_last_price > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_settlement_previous_last_price_positive",
        "instrument_settlement_price",
        type_="check",
    )
    op.drop_column("instrument_settlement_price", "previous_source_event_id")
    op.drop_column(
        "instrument_settlement_price", "previous_source_tick_trading_day"
    )
    op.drop_column("instrument_settlement_price", "previous_source_tick_time")
    op.drop_column("instrument_settlement_price", "previous_last_price")
