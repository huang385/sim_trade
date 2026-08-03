"""扩展订单、成交和持仓的期权资金与估值审计字段。

Revision ID: 20260730_0011
Revises: 20260730_0010
"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_0011"
down_revision = "20260730_0010"
branch_labels = None
depends_on = None


def _add_common_position_columns(table: str) -> None:
    op.add_column(
        table,
        sa.Column(
            "instrument_type",
            sa.String(32),
            nullable=False,
            server_default="FUTURES",
        ),
    )
    op.add_column(
        table,
        sa.Column(
            "initial_occupied_margin",
            sa.Numeric(24, 6),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        table,
        sa.Column(
            "realtime_required_margin",
            sa.Numeric(24, 6),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        table, sa.Column("margin_rule_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        table, sa.Column("margin_rule_version", sa.String(64), nullable=True)
    )
    op.add_column(
        table, sa.Column("margin_rule_snapshot", sa.JSON(), nullable=True)
    )
    op.add_column(
        table, sa.Column("margin_price_mode", sa.String(32), nullable=True)
    )
    op.add_column(
        table,
        sa.Column("margin_underlying_price", sa.Numeric(24, 6), nullable=True),
    )
    op.add_column(
        table,
        sa.Column("margin_option_price", sa.Numeric(24, 6), nullable=True),
    )
    op.add_column(
        table,
        sa.Column("margin_calculated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        table,
        sa.Column("multiplier_snapshot", sa.Numeric(24, 6), nullable=True),
    )
    op.create_index(
        f"ix_{table}_instrument_type",
        table,
        ["instrument_type"],
    )
    op.create_check_constraint(
        f"ck_{table}_initial_margin_nonnegative",
        table,
        "initial_occupied_margin >= 0",
    )
    op.create_check_constraint(
        f"ck_{table}_realtime_margin_nonnegative",
        table,
        "realtime_required_margin >= 0",
    )


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "instrument_type",
            sa.String(32),
            nullable=False,
            server_default="FUTURES",
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "frozen_cash",
            sa.Numeric(24, 6),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "orders", sa.Column("margin_rule_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "orders",
        sa.Column("margin_rule_version", sa.String(64), nullable=True),
    )
    op.add_column(
        "orders", sa.Column("margin_price_mode", sa.String(32), nullable=True)
    )
    op.add_column(
        "orders",
        sa.Column("margin_underlying_price", sa.Numeric(24, 6), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("margin_option_price", sa.Numeric(24, 6), nullable=True),
    )
    op.add_column(
        "orders", sa.Column("margin_rule_snapshot", sa.JSON(), nullable=True)
    )
    op.add_column(
        "orders",
        sa.Column(
            "margin_snapshot_schema_version", sa.String(32), nullable=True
        ),
    )
    op.add_column(
        "orders", sa.Column("fee_rule_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "orders", sa.Column("fee_rule_version", sa.String(64), nullable=True)
    )
    op.add_column(
        "orders", sa.Column("fee_rule_snapshot", sa.JSON(), nullable=True)
    )
    op.add_column(
        "orders",
        sa.Column("margin_calculation_version", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_orders_instrument_type", "orders", ["instrument_type"]
    )
    op.create_check_constraint(
        "ck_order_frozen_cash_nonnegative", "orders", "frozen_cash >= 0"
    )
    op.create_check_constraint(
        "ck_order_margin_underlying_price_nonnegative",
        "orders",
        "margin_underlying_price IS NULL OR margin_underlying_price >= 0",
    )
    op.create_check_constraint(
        "ck_order_margin_option_price_nonnegative",
        "orders",
        "margin_option_price IS NULL OR margin_option_price >= 0",
    )

    op.add_column(
        "trade",
        sa.Column(
            "instrument_type",
            sa.String(32),
            nullable=False,
            server_default="FUTURES",
        ),
    )
    op.add_column(
        "trade",
        sa.Column(
            "premium_cash_flow",
            sa.Numeric(24, 6),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "trade", sa.Column("margin_rule_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "trade",
        sa.Column("margin_rule_version", sa.String(64), nullable=True),
    )
    op.add_column(
        "trade",
        sa.Column("margin_calculation_version", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_trade_instrument_type", "trade", ["instrument_type"]
    )

    _add_common_position_columns("position")
    _add_common_position_columns("position_detail")
    # 现有期货保证金和合约乘数作为新审计字段的回填来源。
    op.execute(
        "UPDATE position SET "
        "initial_occupied_margin = used_margin, "
        "multiplier_snapshot = NULL"
    )
    op.execute(
        "UPDATE position_detail SET "
        "initial_occupied_margin = open_margin, "
        "multiplier_snapshot = 1"
    )
    op.alter_column(
        "position_detail",
        "multiplier_snapshot",
        existing_type=sa.Numeric(24, 6),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_position_detail_multiplier_positive",
        "position_detail",
        "multiplier_snapshot > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_position_detail_multiplier_positive",
        "position_detail",
        type_="check",
    )
    for table in ("position_detail", "position"):
        op.drop_constraint(
            f"ck_{table}_realtime_margin_nonnegative",
            table,
            type_="check",
        )
        op.drop_constraint(
            f"ck_{table}_initial_margin_nonnegative",
            table,
            type_="check",
        )
        op.drop_index(f"ix_{table}_instrument_type", table_name=table)
        # margin_rule_snapshot 是本迁移开发过程中追加的字段。兼容本地曾
        # 执行过早期版本 0011 的数据库，降级时允许该列尚不存在。
        op.execute(
            sa.text(
                f"ALTER TABLE {table} "
                "DROP COLUMN IF EXISTS margin_rule_snapshot"
            )
        )
        for column in (
            "multiplier_snapshot",
            "margin_calculated_at",
            "margin_option_price",
            "margin_underlying_price",
            "margin_price_mode",
            "margin_rule_version",
            "margin_rule_id",
            "realtime_required_margin",
            "initial_occupied_margin",
            "instrument_type",
        ):
            op.drop_column(table, column)

    op.drop_index("ix_trade_instrument_type", table_name="trade")
    for column in (
        "margin_calculation_version",
        "margin_rule_version",
        "margin_rule_id",
        "premium_cash_flow",
        "instrument_type",
    ):
        op.drop_column("trade", column)

    for name in (
        "ck_order_margin_option_price_nonnegative",
        "ck_order_margin_underlying_price_nonnegative",
        "ck_order_frozen_cash_nonnegative",
    ):
        op.drop_constraint(name, "orders", type_="check")
    op.drop_index("ix_orders_instrument_type", table_name="orders")
    for optional_column in (
        "fee_rule_snapshot",
        "fee_rule_version",
        "fee_rule_id",
    ):
        op.execute(
            sa.text(
                "ALTER TABLE orders "
                f"DROP COLUMN IF EXISTS {optional_column}"
            )
        )
    for column in (
        "margin_calculation_version",
        "margin_snapshot_schema_version",
        "margin_rule_snapshot",
        "margin_option_price",
        "margin_underlying_price",
        "margin_price_mode",
        "margin_rule_version",
        "margin_rule_id",
        "frozen_cash",
        "instrument_type",
    ):
        op.drop_column("orders", column)
