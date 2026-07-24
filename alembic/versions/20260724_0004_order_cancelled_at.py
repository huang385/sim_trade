"""为订单增加主动撤销时间

Revision ID: 20260724_0004
Revises: 20260723_0003
Create Date: 2026-07-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260724_0004"
down_revision: Union[str, None] = "20260723_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """只增加可空撤单时间，不改动现有订单数据和其他表。"""

    op.add_column(
        "orders",
        sa.Column(
            "cancelled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """回滚时移除主动撤销时间字段。"""

    op.drop_column("orders", "cancelled_at")
