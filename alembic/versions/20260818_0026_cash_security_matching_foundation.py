"""Add convertible-bond semantics and cash-security fee settlement facts.

Revision ID: 20260818_0026
Revises: 20260817_0025
"""

import sqlalchemy as sa
from alembic import op


revision = "20260818_0026"
down_revision = "20260817_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 统一表继续承载现金证券，但绝不为其伪造衍生品 offset_flag。
    op.drop_constraint("ck_instrument_stock_market_type", "instrument", type_="check")
    op.create_check_constraint(
        "ck_instrument_stock_market_type",
        "instrument",
        "(instrument_type <> 'STOCK' OR market_type = 'STOCK') AND "
        "(instrument_type <> 'CONVERTIBLE_BOND' OR market_type = 'BOND')",
    )
    op.drop_constraint("ck_instrument_stock_multiplier_one", "instrument", type_="check")
    op.create_check_constraint(
        "ck_instrument_stock_multiplier_one",
        "instrument",
        "instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND') OR contract_multiplier = 1",
    )
    op.drop_constraint("ck_instrument_stock_option_fields_empty", "instrument", type_="check")
    op.create_check_constraint(
        "ck_instrument_stock_option_fields_empty",
        "instrument",
        "instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND') OR ("
        "underlying_instrument_id IS NULL AND option_type IS NULL AND "
        "strike_price IS NULL AND exercise_style IS NULL AND settlement_type IS NULL)",
    )

    op.drop_constraint("ck_order_stock_offset_flag_semantics", "orders", type_="check")
    op.create_check_constraint(
        "ck_order_stock_offset_flag_semantics",
        "orders",
        "(instrument_type IN ('STOCK', 'CONVERTIBLE_BOND') AND offset_flag IS NULL) OR "
        "(instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND') AND offset_flag IS NOT NULL)",
    )
    op.drop_constraint("ck_order_stock_fee_snapshot_semantics", "orders", type_="check")
    op.create_check_constraint(
        "ck_order_stock_fee_snapshot_semantics",
        "orders",
        "(instrument_type IN ('STOCK', 'CONVERTIBLE_BOND') AND commission_type IS NULL "
        "AND commission_parameter IS NULL AND commission_contract_multiplier IS NULL) OR "
        "(instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND') AND commission_type IS NOT NULL "
        "AND commission_parameter IS NOT NULL AND commission_contract_multiplier IS NOT NULL)",
    )
    op.drop_constraint("ck_trade_stock_offset_flag_semantics", "trade", type_="check")
    op.create_check_constraint(
        "ck_trade_stock_offset_flag_semantics",
        "trade",
        "(instrument_type IN ('STOCK', 'CONVERTIBLE_BOND') AND offset_flag IS NULL) OR "
        "(instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND') AND offset_flag IS NOT NULL)",
    )
    op.drop_constraint("ck_fee_rule_item_stock_offset_semantics", "fee_rule_item", type_="check")
    op.create_check_constraint(
        "ck_fee_rule_item_stock_offset_semantics",
        "fee_rule_item",
        "(instrument_type IN ('STOCK', 'CONVERTIBLE_BOND') AND offset_flag IS NULL) OR "
        "(instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND') AND offset_flag IS NOT NULL)",
    )

    op.create_table(
        "cash_security_order_fee_accumulator",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("fee_type", sa.String(32), nullable=False),
        sa.Column("cumulative_volume", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cumulative_turnover", sa.Numeric(24, 6), nullable=False, server_default="0"),
        sa.Column("charged_fee", sa.Numeric(24, 6), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("cumulative_volume >= 0", name="ck_cash_fee_acc_volume_nonnegative"),
        sa.CheckConstraint("cumulative_turnover >= 0", name="ck_cash_fee_acc_turnover_nonnegative"),
        sa.CheckConstraint("charged_fee >= 0", name="ck_cash_fee_acc_charged_nonnegative"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.order_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "fee_type", name="uq_cash_fee_acc_order_type"),
    )
    op.create_index("ix_cash_fee_acc_order", "cash_security_order_fee_accumulator", ["order_id"])
    op.create_table(
        "cash_security_trade_fee_component",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trade_id", sa.String(64), nullable=False),
        sa.Column("fee_type", sa.String(32), nullable=False),
        sa.Column("fee_amount", sa.Numeric(24, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("fee_amount >= 0", name="ck_cash_trade_fee_amount_nonnegative"),
        sa.ForeignKeyConstraint(["trade_id"], ["trade.trade_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trade_id", "fee_type", name="uq_cash_trade_fee_component"),
    )
    op.create_index("ix_cash_trade_fee_component_trade", "cash_security_trade_fee_component", ["trade_id"])


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM instrument WHERE instrument_type = 'CONVERTIBLE_BOND') "
        "OR EXISTS (SELECT 1 FROM orders WHERE instrument_type = 'CONVERTIBLE_BOND') "
        "OR EXISTS (SELECT 1 FROM trade WHERE instrument_type = 'CONVERTIBLE_BOND') "
        "OR EXISTS (SELECT 1 FROM cash_security_order_fee_accumulator) "
        "OR EXISTS (SELECT 1 FROM cash_security_trade_fee_component) "
        "THEN RAISE EXCEPTION 'cash-security matching data makes downgrade unsafe'; END IF; END $$;"
    )
    op.drop_index("ix_cash_trade_fee_component_trade", table_name="cash_security_trade_fee_component")
    op.drop_table("cash_security_trade_fee_component")
    op.drop_index("ix_cash_fee_acc_order", table_name="cash_security_order_fee_accumulator")
    op.drop_table("cash_security_order_fee_accumulator")
    # Downgrade keeps the historical STOCK-only contracts after data absence is verified.
    op.drop_constraint("ck_fee_rule_item_stock_offset_semantics", "fee_rule_item", type_="check")
    op.create_check_constraint("ck_fee_rule_item_stock_offset_semantics", "fee_rule_item", "(instrument_type = 'STOCK' AND offset_flag IS NULL) OR (instrument_type <> 'STOCK' AND offset_flag IS NOT NULL)")
    op.drop_constraint("ck_trade_stock_offset_flag_semantics", "trade", type_="check")
    op.create_check_constraint("ck_trade_stock_offset_flag_semantics", "trade", "(instrument_type = 'STOCK' AND offset_flag IS NULL) OR (instrument_type <> 'STOCK' AND offset_flag IS NOT NULL)")
    op.drop_constraint("ck_order_stock_fee_snapshot_semantics", "orders", type_="check")
    op.create_check_constraint("ck_order_stock_fee_snapshot_semantics", "orders", "(instrument_type = 'STOCK' AND commission_type IS NULL AND commission_parameter IS NULL AND commission_contract_multiplier IS NULL) OR (instrument_type <> 'STOCK' AND commission_type IS NOT NULL AND commission_parameter IS NOT NULL AND commission_contract_multiplier IS NOT NULL)")
    op.drop_constraint("ck_order_stock_offset_flag_semantics", "orders", type_="check")
    op.create_check_constraint("ck_order_stock_offset_flag_semantics", "orders", "(instrument_type = 'STOCK' AND offset_flag IS NULL) OR (instrument_type <> 'STOCK' AND offset_flag IS NOT NULL)")
    op.drop_constraint("ck_instrument_stock_option_fields_empty", "instrument", type_="check")
    op.create_check_constraint("ck_instrument_stock_option_fields_empty", "instrument", "instrument_type <> 'STOCK' OR (underlying_instrument_id IS NULL AND option_type IS NULL AND strike_price IS NULL AND exercise_style IS NULL AND settlement_type IS NULL)")
    op.drop_constraint("ck_instrument_stock_multiplier_one", "instrument", type_="check")
    op.create_check_constraint("ck_instrument_stock_multiplier_one", "instrument", "instrument_type <> 'STOCK' OR contract_multiplier = 1")
    op.drop_constraint("ck_instrument_stock_market_type", "instrument", type_="check")
    op.create_check_constraint("ck_instrument_stock_market_type", "instrument", "instrument_type <> 'STOCK' OR market_type = 'STOCK'")
