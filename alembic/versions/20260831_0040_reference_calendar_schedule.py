"""Add reference-data calendar and product trading schedule tables.

Revision ID: 20260831_0040
Revises: 20260831_0039
"""

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260831_0040"
down_revision = "20260831_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trading_calendar",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("exchange_id", sa.String(length=32), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("previous_trading_day", sa.Date(), nullable=True),
        sa.Column("next_trading_day", sa.Date(), nullable=True),
        sa.Column("is_open", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("special_reason", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'CLOSED', 'SPECIAL')",
            name="ck_trading_calendar_status",
        ),
        sa.CheckConstraint(
            "previous_trading_day IS NULL OR previous_trading_day < trading_day",
            name="ck_trading_calendar_previous",
        ),
        sa.CheckConstraint(
            "next_trading_day IS NULL OR next_trading_day > trading_day",
            name="ck_trading_calendar_next",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "exchange_id", "trading_day", name="uq_trading_calendar_exchange_day"
        ),
    )
    op.create_index(
        "ix_trading_calendar_exchange_id", "trading_calendar", ["exchange_id"]
    )
    op.create_index(
        "ix_trading_calendar_trading_day", "trading_calendar", ["trading_day"]
    )
    op.create_index(
        "ix_trading_calendar_day_open",
        "trading_calendar",
        ["trading_day", "is_open"],
    )

    op.create_table(
        "product_trading_schedule",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("exchange_id", sa.String(length=32), nullable=False),
        sa.Column("product_code", sa.String(length=64), nullable=False),
        sa.Column("instrument_type", sa.String(length=32), nullable=False),
        sa.Column("sessions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("representative_order_book_id", sa.String(length=64), nullable=False),
        sa.Column("schedule_hash", sa.String(length=64), nullable=False),
        sa.Column("sync_batch_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'CLOSED', 'SPECIAL')",
            name="ck_product_schedule_status",
        ),
        sa.CheckConstraint(
            "instrument_type IN ('FUTURES', 'FUTURES_OPTION', 'INDEX_OPTION')",
            name="ck_product_schedule_instrument_type",
        ),
        sa.CheckConstraint("version >= 1", name="ck_product_schedule_version"),
        sa.CheckConstraint(
            "jsonb_typeof(sessions) = 'array'",
            name="ck_product_schedule_sessions_array",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "trading_day",
            "exchange_id",
            "product_code",
            "instrument_type",
            name="uq_product_schedule_day_exchange_product_type",
        ),
    )
    op.create_index(
        "ix_product_trading_schedule_trading_day",
        "product_trading_schedule",
        ["trading_day"],
    )
    op.create_index(
        "ix_product_trading_schedule_exchange_id",
        "product_trading_schedule",
        ["exchange_id"],
    )
    op.create_index(
        "ix_product_trading_schedule_product_code",
        "product_trading_schedule",
        ["product_code"],
    )
    op.create_index(
        "ix_product_trading_schedule_instrument_type",
        "product_trading_schedule",
        ["instrument_type"],
    )
    op.create_index(
        "ix_product_trading_schedule_sync_batch_id",
        "product_trading_schedule",
        ["sync_batch_id"],
    )
    op.create_index(
        "ix_product_schedule_lookup",
        "product_trading_schedule",
        ["trading_day", "exchange_id", "product_code", "instrument_type"],
    )


def downgrade() -> None:
    op.drop_table("product_trading_schedule")
    op.drop_table("trading_calendar")
