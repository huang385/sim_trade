"""增加手续费快照和平仓成交逐笔持仓审计

Revision ID: 20260727_0006
Revises: 20260724_0005
Create Date: 2026-07-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0006"
down_revision: Union[str, None] = "20260724_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    建立新订单所需的手续费快照和逐笔平仓审计结构。

    当前本地数据库只保存可删除的测试数据，本迁移按空测试库设计，不再
    猜测旧订单手续费规则、修补旧账户资金或伪造旧成交审计明细。
    """

    # 订单保存接受时的手续费规则快照。预计冻结使用限价，实际成交使用
    # 成交价，但两者始终使用这组三字段，避免规则更新改变历史订单口径。
    op.add_column(
        "orders",
        sa.Column("commission_type", sa.String(32), nullable=False),
    )
    op.add_column(
        "orders",
        sa.Column(
            "commission_parameter",
            sa.Numeric(24, 12),
            nullable=False,
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "commission_contract_multiplier",
            sa.Numeric(24, 6),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_order_commission_parameter_nonnegative",
        "orders",
        "commission_parameter >= 0",
    )
    op.create_check_constraint(
        "ck_order_commission_multiplier_positive",
        "orders",
        "commission_contract_multiplier > 0",
    )

    # Allocation同时保存明确的平今/平昨结果、手续费规则快照，以及预计
    # 冻结手续费的剩余、成交消费和撤单释放资源流。
    allocation_columns = (
        sa.Column("resolved_offset_flag", sa.String(32), nullable=False),
        sa.Column("commission_type", sa.String(32), nullable=False),
        sa.Column(
            "commission_parameter",
            sa.Numeric(24, 12),
            nullable=False,
        ),
        sa.Column(
            "commission_contract_multiplier",
            sa.Numeric(24, 6),
            nullable=False,
        ),
        sa.Column(
            "original_frozen_commission",
            sa.Numeric(24, 6),
            nullable=False,
        ),
        sa.Column(
            "remaining_frozen_commission",
            sa.Numeric(24, 6),
            nullable=False,
        ),
        sa.Column(
            "consumed_commission",
            sa.Numeric(24, 6),
            nullable=False,
        ),
        sa.Column(
            "released_commission",
            sa.Numeric(24, 6),
            nullable=False,
        ),
    )
    for column in allocation_columns:
        op.add_column("position_freeze_allocation", column)

    op.create_check_constraint(
        "ck_position_freeze_resolved_offset",
        "position_freeze_allocation",
        "resolved_offset_flag IN ('CLOSE_TODAY', 'CLOSE_YESTERDAY')",
    )
    op.create_check_constraint(
        "ck_position_freeze_commission_parameter_nonnegative",
        "position_freeze_allocation",
        "commission_parameter >= 0",
    )
    op.create_check_constraint(
        "ck_position_freeze_commission_multiplier_positive",
        "position_freeze_allocation",
        "commission_contract_multiplier > 0",
    )
    op.create_check_constraint(
        "ck_position_freeze_commission_nonnegative",
        "position_freeze_allocation",
        "original_frozen_commission >= 0 "
        "AND remaining_frozen_commission >= 0 "
        "AND consumed_commission >= 0 "
        "AND released_commission >= 0",
    )
    op.create_check_constraint(
        "ck_position_freeze_commission_balance",
        "position_freeze_allocation",
        "original_frozen_commission = remaining_frozen_commission "
        "+ consumed_commission + released_commission",
    )

    # 每一笔平仓Trade保存实际消费的持仓明细、保证金、手续费和盈亏，
    # 使跨多条PositionDetail、跨平今/平昨的成交可以完整审计。
    op.create_table(
        "trade_position_allocation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "trade_position_allocation_id",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("trade_id", sa.String(64), nullable=False),
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("allocation_id", sa.String(64), nullable=False),
        sa.Column("position_id", sa.String(64), nullable=False),
        sa.Column("position_detail_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("order_book_id", sa.String(64), nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("resolved_offset_flag", sa.String(32), nullable=False),
        sa.Column("open_trading_day", sa.Date(), nullable=False),
        sa.Column("close_trading_day", sa.Date(), nullable=False),
        sa.Column("open_price", sa.Numeric(24, 6), nullable=False),
        sa.Column("close_price", sa.Numeric(24, 6), nullable=False),
        sa.Column("close_volume", sa.Integer(), nullable=False),
        sa.Column("released_margin", sa.Numeric(24, 6), nullable=False),
        sa.Column("commission", sa.Numeric(24, 6), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(24, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_trade_position_allocation"),
        sa.UniqueConstraint(
            "trade_position_allocation_id",
            name="uq_trade_position_allocation_id",
        ),
        sa.UniqueConstraint(
            "trade_id",
            "allocation_id",
            name="uq_trade_position_trade_allocation",
        ),
        sa.CheckConstraint(
            "resolved_offset_flag IN ('CLOSE_TODAY', 'CLOSE_YESTERDAY')",
            name="ck_trade_position_resolved_offset",
        ),
        sa.CheckConstraint(
            "close_volume > 0",
            name="ck_trade_position_close_volume_positive",
        ),
        sa.CheckConstraint(
            "released_margin >= 0",
            name="ck_trade_position_margin_nonnegative",
        ),
        sa.CheckConstraint(
            "commission >= 0",
            name="ck_trade_position_commission_nonnegative",
        ),
    )
    op.create_index(
        "ix_trade_position_allocation_trade_id",
        "trade_position_allocation",
        ["trade_id"],
    )
    op.create_index(
        "ix_trade_position_order_id",
        "trade_position_allocation",
        ["order_id"],
    )
    op.create_index(
        "ix_trade_position_allocation_position_id",
        "trade_position_allocation",
        ["position_id"],
    )
    op.create_index(
        "ix_trade_position_position_detail_id",
        "trade_position_allocation",
        ["position_detail_id"],
    )
    op.create_index(
        "ix_trade_position_allocation_account_id",
        "trade_position_allocation",
        ["account_id"],
    )


def downgrade() -> None:
    """移除逐笔审计、Allocation手续费资源和订单手续费快照。"""

    op.drop_table("trade_position_allocation")
    for constraint in (
        "ck_position_freeze_commission_balance",
        "ck_position_freeze_commission_nonnegative",
        "ck_position_freeze_commission_multiplier_positive",
        "ck_position_freeze_commission_parameter_nonnegative",
        "ck_position_freeze_resolved_offset",
    ):
        op.drop_constraint(
            constraint,
            "position_freeze_allocation",
            type_="check",
        )
    for column in (
        "released_commission",
        "consumed_commission",
        "remaining_frozen_commission",
        "original_frozen_commission",
        "commission_contract_multiplier",
        "commission_parameter",
        "commission_type",
        "resolved_offset_flag",
    ):
        op.drop_column("position_freeze_allocation", column)

    op.drop_constraint(
        "ck_order_commission_multiplier_positive",
        "orders",
        type_="check",
    )
    op.drop_constraint(
        "ck_order_commission_parameter_nonnegative",
        "orders",
        type_="check",
    )
    op.drop_column("orders", "commission_contract_multiplier")
    op.drop_column("orders", "commission_parameter")
    op.drop_column("orders", "commission_type")
