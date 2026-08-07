"""扩展不可变成交回放与日终守恒事实。

Revision ID: 20260806_0019
Revises: 20260806_0018
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260806_0019"
down_revision = "20260806_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account",
        sa.Column(
            "cumulative_net_pnl",
            sa.Numeric(24, 6),
            nullable=False,
            server_default="0",
        ),
    )

    account_money_columns = (
        "opening_cash_balance",
        "trade_cash_flow",
        "futures_close_pnl",
        "option_economic_pnl",
        "option_premium_cash_flow",
        "daily_close_pnl",
        "daily_net_pnl",
    )
    for name in account_money_columns:
        op.add_column(
            "daily_account_settlement",
            sa.Column(
                name,
                sa.Numeric(24, 6),
                nullable=False,
                server_default="0",
            ),
        )
    op.add_column(
        "daily_account_settlement",
        sa.Column(
            "reconciliation_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    position_volume_columns = (
        "opening_yesterday_volume",
        "today_open_volume",
        "today_close_volume",
        "today_close_today_volume",
        "today_close_yesterday_volume",
    )
    for name in position_volume_columns:
        op.add_column(
            "daily_position_settlement",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )
    position_money_columns = (
        "close_pnl",
        "option_economic_pnl",
        "commission",
        "premium_cash_flow",
        "cumulative_economic_pnl",
    )
    for name in position_money_columns:
        op.add_column(
            "daily_position_settlement",
            sa.Column(
                name,
                sa.Numeric(24, 6),
                nullable=False,
                server_default="0",
            ),
        )

    op.add_column(
        "option_expiry_settlement_detail",
        sa.Column(
            "realized_pnl",
            sa.Numeric(24, 6),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_unique_constraint(
        "uq_option_expiry_settlement_position_once",
        "option_expiry_settlement_detail",
        ["position_id"],
    )


def downgrade() -> None:
    # 新字段已经成为不可重建的资金守恒和成交回放审计事实。自动删除会让
    # 已完成批次失去解释能力，因此必须先独立归档后再编写受控迁移。
    raise RuntimeError("日终成交回放事实不可逆，禁止自动 downgrade")

