"""Add ETF secondary-market reference fields and cash-security semantics.

Revision ID: 20260903_0042
Revises: 20260831_0041
"""

import sqlalchemy as sa

from alembic import op


revision = "20260903_0042"
down_revision = "20260831_0041"
branch_labels = None
depends_on = None


def _replace_check(table: str, name: str, sqltext: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, sqltext)


def upgrade() -> None:
    op.add_column("instrument", sa.Column("fund_type", sa.String(32), nullable=True))
    op.add_column("instrument", sa.Column("market_tplus", sa.Integer(), nullable=True))
    op.add_column("instrument", sa.Column("round_lot", sa.Integer(), nullable=True))
    op.add_column("instrument", sa.Column("least_redeem", sa.Integer(), nullable=True))
    op.add_column(
        "instrument",
        sa.Column("reference_underlying_order_book_id", sa.String(64), nullable=True),
    )
    _replace_check(
        "instrument", "ck_instrument_stock_market_type",
        "(instrument_type <> 'STOCK' OR market_type = 'STOCK') AND "
        "(instrument_type <> 'CONVERTIBLE_BOND' OR market_type = 'BOND') AND "
        "(instrument_type <> 'ETF' OR market_type = 'FUND')",
    )
    _replace_check(
        "instrument", "ck_instrument_stock_multiplier_one",
        "instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND', 'ETF') "
        "OR contract_multiplier = 1",
    )
    _replace_check(
        "instrument", "ck_instrument_stock_option_fields_empty",
        "instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND', 'ETF') OR ("
        "underlying_instrument_id IS NULL AND option_type IS NULL AND "
        "strike_price IS NULL AND exercise_style IS NULL AND settlement_type IS NULL)",
    )
    op.create_check_constraint(
        "ck_instrument_etf_reference_fields", "instrument",
        "instrument_type <> 'ETF' OR (fund_type IS NOT NULL AND "
        "market_tplus IS NOT NULL AND market_tplus IN (0, 1) AND "
        "round_lot IS NOT NULL AND round_lot > 0)",
    )
    _replace_check(
        "orders", "ck_order_stock_offset_flag_semantics",
        "(instrument_type IN ('STOCK', 'CONVERTIBLE_BOND', 'ETF') AND offset_flag IS NULL) "
        "OR (instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND', 'ETF') "
        "AND offset_flag IS NOT NULL)",
    )
    _replace_check(
        "orders", "ck_order_stock_fee_snapshot_semantics",
        "(instrument_type IN ('STOCK', 'CONVERTIBLE_BOND', 'ETF') "
        "AND commission_type IS NULL AND commission_parameter IS NULL "
        "AND commission_contract_multiplier IS NULL) OR "
        "(instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND', 'ETF') "
        "AND commission_type IS NOT NULL AND commission_parameter IS NOT NULL "
        "AND commission_contract_multiplier IS NOT NULL)",
    )
    _replace_check(
        "trade", "ck_trade_stock_offset_flag_semantics",
        "(instrument_type IN ('STOCK', 'CONVERTIBLE_BOND', 'ETF') AND offset_flag IS NULL) "
        "OR (instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND', 'ETF') "
        "AND offset_flag IS NOT NULL)",
    )
    _replace_check(
        "fee_rule_item", "ck_fee_rule_item_stock_offset_semantics",
        "(instrument_type IN ('STOCK', 'CONVERTIBLE_BOND', 'ETF') AND offset_flag IS NULL) "
        "OR (instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND', 'ETF') "
        "AND offset_flag IS NOT NULL)",
    )


def downgrade() -> None:
    # A downgrade cannot preserve ETF orders under the legacy constraints.
    op.execute("DELETE FROM fee_rule_item WHERE instrument_type = 'ETF'")
    op.execute("DELETE FROM trade WHERE instrument_type = 'ETF'")
    op.execute("DELETE FROM orders WHERE instrument_type = 'ETF'")
    op.execute("DELETE FROM stock_daily_trading_fact WHERE instrument_id IN "
               "(SELECT id FROM instrument WHERE instrument_type = 'ETF')")
    op.execute("DELETE FROM stock_trading_rule WHERE instrument_id IN "
               "(SELECT id FROM instrument WHERE instrument_type = 'ETF')")
    op.execute("DELETE FROM instrument WHERE instrument_type = 'ETF'")
    op.drop_constraint("ck_instrument_etf_reference_fields", "instrument", type_="check")
    _replace_check(
        "fee_rule_item", "ck_fee_rule_item_stock_offset_semantics",
        "(instrument_type IN ('STOCK', 'CONVERTIBLE_BOND') AND offset_flag IS NULL) OR "
        "(instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND') AND offset_flag IS NOT NULL)",
    )
    _replace_check(
        "trade", "ck_trade_stock_offset_flag_semantics",
        "(instrument_type IN ('STOCK', 'CONVERTIBLE_BOND') AND offset_flag IS NULL) OR "
        "(instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND') AND offset_flag IS NOT NULL)",
    )
    _replace_check(
        "orders", "ck_order_stock_fee_snapshot_semantics",
        "(instrument_type IN ('STOCK', 'CONVERTIBLE_BOND') AND commission_type IS NULL "
        "AND commission_parameter IS NULL AND commission_contract_multiplier IS NULL) OR "
        "(instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND') AND commission_type IS NOT NULL "
        "AND commission_parameter IS NOT NULL AND commission_contract_multiplier IS NOT NULL)",
    )
    _replace_check(
        "orders", "ck_order_stock_offset_flag_semantics",
        "(instrument_type IN ('STOCK', 'CONVERTIBLE_BOND') AND offset_flag IS NULL) OR "
        "(instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND') AND offset_flag IS NOT NULL)",
    )
    _replace_check(
        "instrument", "ck_instrument_stock_option_fields_empty",
        "instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND') OR ("
        "underlying_instrument_id IS NULL AND option_type IS NULL AND strike_price IS NULL "
        "AND exercise_style IS NULL AND settlement_type IS NULL)",
    )
    _replace_check(
        "instrument", "ck_instrument_stock_multiplier_one",
        "instrument_type NOT IN ('STOCK', 'CONVERTIBLE_BOND') OR contract_multiplier = 1",
    )
    _replace_check(
        "instrument", "ck_instrument_stock_market_type",
        "(instrument_type <> 'STOCK' OR market_type = 'STOCK') AND "
        "(instrument_type <> 'CONVERTIBLE_BOND' OR market_type = 'BOND')",
    )
    for column in (
        "reference_underlying_order_book_id", "least_redeem", "round_lot",
        "market_tplus", "fund_type",
    ):
        op.drop_column("instrument", column)
