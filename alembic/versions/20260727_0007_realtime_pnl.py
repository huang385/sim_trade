"""增加盘中实时盈亏与当日盈亏字段

Revision ID: 20260727_0007
Revises: 20260727_0006
Create Date: 2026-07-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0007"
down_revision: Union[str, None] = "20260727_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_money_column(table_name: str, column_name: str) -> None:
    """用零回填已有测试数据，再移除数据库默认值。"""

    op.add_column(
        table_name,
        sa.Column(
            column_name,
            sa.Numeric(24, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.alter_column(
        table_name,
        column_name,
        server_default=None,
    )


def upgrade() -> None:
    """建立累计/当日盈亏分离所需的持久化字段。"""

    for column_name in (
        "daily_position_pnl",
        "daily_close_pnl",
        "daily_commission",
    ):
        _add_money_column("account", column_name)

    for column_name in (
        "daily_position_pnl",
        "daily_close_pnl",
    ):
        _add_money_column("position", column_name)

    # 当前尚未执行日终结算，现有测试持仓以真实开仓价作为当日盈亏基准。
    op.add_column(
        "position_detail",
        sa.Column("pnl_base_price", sa.Numeric(24, 6), nullable=True),
    )
    op.execute(
        "UPDATE position_detail "
        "SET pnl_base_price = open_price "
        "WHERE pnl_base_price IS NULL"
    )
    op.alter_column(
        "position_detail",
        "pnl_base_price",
        nullable=False,
    )
    op.create_check_constraint(
        "ck_position_detail_pnl_base_price_positive",
        "position_detail",
        "pnl_base_price > 0",
    )

    _add_money_column("trade", "daily_close_pnl")
    _add_money_column(
        "trade_position_allocation",
        "daily_close_pnl",
    )


def downgrade() -> None:
    """移除本阶段新增的实时与当日盈亏字段。"""

    op.drop_column(
        "trade_position_allocation",
        "daily_close_pnl",
    )
    op.drop_column("trade", "daily_close_pnl")
    op.drop_constraint(
        "ck_position_detail_pnl_base_price_positive",
        "position_detail",
        type_="check",
    )
    op.drop_column("position_detail", "pnl_base_price")
    op.drop_column("position", "daily_close_pnl")
    op.drop_column("position", "daily_position_pnl")
    op.drop_column("account", "daily_commission")
    op.drop_column("account", "daily_close_pnl")
    op.drop_column("account", "daily_position_pnl")
