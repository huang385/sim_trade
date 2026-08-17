"""Add cash-stock order entry, fee components, and immutable snapshots.

Revision ID: 20260817_0025
Revises: 20260817_0024
"""

import sqlalchemy as sa
from alembic import op


revision = "20260817_0025"
down_revision = "20260817_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fee_rule_item",
        sa.Column(
            "fee_type",
            sa.String(32),
            nullable=False,
            server_default="DERIVATIVE_COMMISSION",
        ),
    )
    op.add_column(
        "fee_rule_item",
        sa.Column("minimum_fee", sa.Numeric(24, 6), nullable=False, server_default="0"),
    )
    op.add_column(
        "fee_rule_item",
        sa.Column(
            "aggregation_scope", sa.String(16), nullable=False, server_default="TRADE"
        ),
    )
    op.alter_column("fee_rule_item", "offset_flag", nullable=True)
    op.drop_constraint("uq_fee_rule_item_scope_version", "fee_rule_item", type_="unique")
    op.create_unique_constraint(
        "uq_fee_rule_item_scope_version",
        "fee_rule_item",
        [
            "exchange_id",
            "product_id",
            "instrument_id",
            "instrument_type",
            "direction",
            "offset_flag",
            "fee_type",
            "trading_day",
            "rule_version",
        ],
        postgresql_nulls_not_distinct=True,
    )
    op.create_check_constraint(
        "ck_fee_rule_item_minimum_fee_nonnegative",
        "fee_rule_item",
        "minimum_fee >= 0",
    )
    op.create_check_constraint(
        "ck_fee_rule_item_fee_type_valid",
        "fee_rule_item",
        "fee_type IN ('DERIVATIVE_COMMISSION', 'BROKER_COMMISSION', "
        "'STAMP_DUTY', 'TRANSFER_FEE', 'HANDLING_FEE', 'OTHER')",
    )
    op.create_check_constraint(
        "ck_fee_rule_item_aggregation_scope_valid",
        "fee_rule_item",
        "aggregation_scope IN ('ORDER', 'TRADE')",
    )
    op.create_check_constraint(
        "ck_fee_rule_item_stock_offset_semantics",
        "fee_rule_item",
        "(instrument_type = 'STOCK' AND offset_flag IS NULL) OR "
        "(instrument_type <> 'STOCK' AND offset_flag IS NOT NULL)",
    )
    op.alter_column("fee_rule_item", "fee_type", server_default=None)
    op.alter_column("fee_rule_item", "minimum_fee", server_default=None)
    op.alter_column("fee_rule_item", "aggregation_scope", server_default=None)

    op.alter_column("orders", "commission_type", nullable=True)
    op.alter_column("orders", "commission_parameter", nullable=True)
    op.alter_column("orders", "commission_contract_multiplier", nullable=True)
    op.create_check_constraint(
        "ck_order_stock_fee_snapshot_semantics",
        "orders",
        "(instrument_type = 'STOCK' AND commission_type IS NULL "
        "AND commission_parameter IS NULL "
        "AND commission_contract_multiplier IS NULL) OR "
        "(instrument_type <> 'STOCK' AND commission_type IS NOT NULL "
        "AND commission_parameter IS NOT NULL "
        "AND commission_contract_multiplier IS NOT NULL)",
    )

    op.create_table(
        "order_fee_component_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("fee_type", sa.String(32), nullable=False),
        sa.Column("rule_item_id", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("calculation_type", sa.String(32), nullable=False),
        sa.Column("commission_parameter", sa.Numeric(24, 12), nullable=False),
        sa.Column("minimum_fee", sa.Numeric(24, 6), nullable=False),
        sa.Column("aggregation_scope", sa.String(16), nullable=False),
        sa.Column("contract_multiplier", sa.Numeric(24, 6), nullable=False),
        sa.Column("data_source", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("minimum_fee >= 0", name="ck_order_fee_snapshot_minimum_nonnegative"),
        sa.CheckConstraint("contract_multiplier > 0", name="ck_order_fee_snapshot_multiplier_positive"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.order_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["rule_item_id"], ["fee_rule_item.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "fee_type", name="uq_order_fee_component_type"),
    )
    op.create_index(
        "ix_order_fee_component_snapshot_order",
        "order_fee_component_snapshot",
        ["order_id"],
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM order_fee_component_snapshot) "
        "OR EXISTS (SELECT 1 FROM orders WHERE instrument_type = 'STOCK') "
        "OR EXISTS (SELECT 1 FROM fee_rule_item WHERE instrument_type = 'STOCK') "
        "THEN RAISE EXCEPTION 'stock order entry data makes downgrade unsafe'; "
        "END IF; END $$;"
    )
    op.drop_index("ix_order_fee_component_snapshot_order", table_name="order_fee_component_snapshot")
    op.drop_table("order_fee_component_snapshot")
    op.drop_constraint("ck_order_stock_fee_snapshot_semantics", "orders", type_="check")
    op.alter_column("orders", "commission_contract_multiplier", nullable=False)
    op.alter_column("orders", "commission_parameter", nullable=False)
    op.alter_column("orders", "commission_type", nullable=False)
    op.drop_constraint("ck_fee_rule_item_stock_offset_semantics", "fee_rule_item", type_="check")
    op.drop_constraint("ck_fee_rule_item_aggregation_scope_valid", "fee_rule_item", type_="check")
    op.drop_constraint("ck_fee_rule_item_fee_type_valid", "fee_rule_item", type_="check")
    op.drop_constraint("ck_fee_rule_item_minimum_fee_nonnegative", "fee_rule_item", type_="check")
    op.drop_constraint("uq_fee_rule_item_scope_version", "fee_rule_item", type_="unique")
    op.create_unique_constraint(
        "uq_fee_rule_item_scope_version",
        "fee_rule_item",
        ["exchange_id", "product_id", "instrument_id", "instrument_type", "direction", "offset_flag", "trading_day", "rule_version"],
        postgresql_nulls_not_distinct=True,
    )
    op.alter_column("fee_rule_item", "offset_flag", nullable=False)
    op.drop_column("fee_rule_item", "aggregation_scope")
    op.drop_column("fee_rule_item", "minimum_fee")
    op.drop_column("fee_rule_item", "fee_type")
