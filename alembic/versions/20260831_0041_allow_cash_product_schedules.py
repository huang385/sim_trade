"""Allow cash-security instrument types in product trading schedules.

The reference synchronizer now creates schedules for stocks and convertible
bonds as well as futures and options, so the legacy finite-type constraint is
no longer compatible with the producer's model.

Revision ID: 20260831_0041
Revises: 20260831_0040
"""

import sqlalchemy as sa

from alembic import op


revision = "20260831_0041"
down_revision = "20260831_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_product_schedule_instrument_type",
        "product_trading_schedule",
        type_="check",
    )


def downgrade() -> None:
    op.create_check_constraint(
        "ck_product_schedule_instrument_type",
        "product_trading_schedule",
        sa.column("instrument_type").in_(
            ("FUTURES", "FUTURES_OPTION", "INDEX_OPTION")
        ),
    )
