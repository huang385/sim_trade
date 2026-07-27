"""增加平仓逐笔冻结分配和持仓剩余保证金

Revision ID: 20260724_0005
Revises: 20260724_0004
Create Date: 2026-07-24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260724_0005"
down_revision: Union[str, None] = "20260724_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """回填剩余保证金并建立订单到逐笔持仓的冻结分配表。"""

    op.add_column(
        "position_detail",
        sa.Column("remaining_margin", sa.Numeric(24, 6), nullable=True),
    )
    op.execute(
        "UPDATE position_detail "
        "SET remaining_margin = open_margin "
        "WHERE remaining_margin IS NULL"
    )
    op.alter_column(
        "position_detail",
        "remaining_margin",
        existing_type=sa.Numeric(24, 6),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_position_detail_remaining_margin_nonnegative",
        "position_detail",
        "remaining_margin >= 0",
    )

    op.create_table(
        "position_freeze_allocation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("allocation_id", sa.String(64), nullable=False),
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("position_id", sa.String(64), nullable=False),
        sa.Column("position_detail_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("offset_flag", sa.String(32), nullable=False),
        sa.Column("original_frozen_volume", sa.Integer(), nullable=False),
        sa.Column("remaining_frozen_volume", sa.Integer(), nullable=False),
        sa.Column("consumed_volume", sa.Integer(), nullable=False),
        sa.Column("released_volume", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "original_frozen_volume > 0",
            name="ck_position_freeze_original_positive",
        ),
        sa.CheckConstraint(
            "remaining_frozen_volume >= 0",
            name="ck_position_freeze_remaining_nonnegative",
        ),
        sa.CheckConstraint(
            "consumed_volume >= 0",
            name="ck_position_freeze_consumed_nonnegative",
        ),
        sa.CheckConstraint(
            "released_volume >= 0",
            name="ck_position_freeze_released_nonnegative",
        ),
        sa.CheckConstraint(
            "original_frozen_volume = remaining_frozen_volume "
            "+ consumed_volume + released_volume",
            name="ck_position_freeze_volume_balance",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_position_freeze_allocation"),
        sa.UniqueConstraint(
            "allocation_id",
            name="uq_position_freeze_allocation_id",
        ),
        sa.UniqueConstraint(
            "order_id",
            "position_detail_id",
            name="uq_position_freeze_order_detail",
        ),
    )
    op.create_index(
        "ix_position_freeze_allocation_order_id",
        "position_freeze_allocation",
        ["order_id"],
    )
    op.create_index(
        "ix_position_freeze_allocation_position_id",
        "position_freeze_allocation",
        ["position_id"],
    )
    op.create_index(
        "ix_position_freeze_allocation_position_detail_id",
        "position_freeze_allocation",
        ["position_detail_id"],
    )
    op.create_index(
        "ix_position_freeze_allocation_account_id",
        "position_freeze_allocation",
        ["account_id"],
    )


def downgrade() -> None:
    """删除平仓冻结分配并移除逐笔剩余保证金字段。"""

    op.drop_table("position_freeze_allocation")
    op.drop_constraint(
        "ck_position_detail_remaining_margin_nonnegative",
        "position_detail",
        type_="check",
    )
    op.drop_column("position_detail", "remaining_margin")
