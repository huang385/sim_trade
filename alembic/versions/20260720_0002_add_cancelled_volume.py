"""为订单预留部分撤销状态和撤销数量

Revision ID: 20260720_0002
Revises: 20260720_0001
Create Date: 2026-07-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0002"
down_revision: Union[str, None] = "20260720_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    增加撤销量并建立数量平衡约束。

    server_default=0 确保现有订单安全补齐字段；本迁移只预留结构，不修改
    订单状态、账户冻结资金，也不创建任何撤单事件。
    """

    op.add_column(
        "orders",
        sa.Column(
            "cancelled_volume",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_order_volume_balance",
        "orders",
        "total_volume = traded_volume + remaining_volume + cancelled_volume",
    )


def downgrade() -> None:
    """回退数量平衡约束和撤销量字段。"""

    op.drop_constraint(
        "ck_order_volume_balance",
        "orders",
        type_="check",
    )
    op.drop_column("orders", "cancelled_volume")
