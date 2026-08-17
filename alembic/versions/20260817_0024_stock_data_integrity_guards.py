"""Strengthen stock reference-data integrity and replay-safe semantics.

Revision ID: 20260817_0024
Revises: 20260817_0023
"""

from alembic import op


revision = "20260817_0024"
down_revision = "20260817_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 已有股票事实若不满足新语义，停止升级而不是静默改写业务数据。
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM stock_trading_rule "
        "WHERE normal_price_limit_ratio = 0 "
        "OR special_price_limit_ratio = 0 "
        "OR price_limit_type NOT IN ('RATIO', 'NONE')) "
        "OR EXISTS (SELECT 1 FROM stock_daily_trading_fact "
        "WHERE (upper_limit_price IS NOT NULL "
        "AND lower_limit_price IS NOT NULL "
        "AND upper_limit_price < lower_limit_price) "
        "OR (is_suspended AND is_tradeable)) "
        "THEN RAISE EXCEPTION 'stock reference data violates new integrity guards'; "
        "END IF; END $$;"
    )

    op.drop_constraint(
        "ck_stock_trading_rule_normal_limit_nonnegative",
        "stock_trading_rule",
        type_="check",
    )
    op.create_check_constraint(
        "ck_stock_trading_rule_normal_limit_nonnegative",
        "stock_trading_rule",
        "normal_price_limit_ratio IS NULL OR normal_price_limit_ratio > 0",
    )
    op.drop_constraint(
        "ck_stock_trading_rule_special_limit_nonnegative",
        "stock_trading_rule",
        type_="check",
    )
    op.create_check_constraint(
        "ck_stock_trading_rule_special_limit_nonnegative",
        "stock_trading_rule",
        "special_price_limit_ratio IS NULL OR special_price_limit_ratio > 0",
    )
    op.create_check_constraint(
        "ck_stock_trading_rule_price_limit_type_valid",
        "stock_trading_rule",
        "price_limit_type IN ('RATIO', 'NONE')",
    )

    op.drop_constraint(
        "ck_stock_daily_trading_fact_limit_prices_valid",
        "stock_daily_trading_fact",
        type_="check",
    )
    op.create_check_constraint(
        "ck_stock_daily_trading_fact_limit_prices_valid",
        "stock_daily_trading_fact",
        "(upper_limit_price IS NULL AND lower_limit_price IS NULL) OR "
        "(upper_limit_price > 0 AND lower_limit_price > 0 AND "
        "upper_limit_price >= lower_limit_price)",
    )
    op.create_check_constraint(
        "ck_stock_daily_trading_fact_suspension_not_tradeable",
        "stock_daily_trading_fact",
        "NOT is_suspended OR NOT is_tradeable",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_stock_daily_trading_fact_suspension_not_tradeable",
        "stock_daily_trading_fact",
        type_="check",
    )
    op.drop_constraint(
        "ck_stock_daily_trading_fact_limit_prices_valid",
        "stock_daily_trading_fact",
        type_="check",
    )
    op.create_check_constraint(
        "ck_stock_daily_trading_fact_limit_prices_valid",
        "stock_daily_trading_fact",
        "(upper_limit_price IS NULL AND lower_limit_price IS NULL) OR "
        "(upper_limit_price > 0 AND lower_limit_price > 0)",
    )

    op.drop_constraint(
        "ck_stock_trading_rule_price_limit_type_valid",
        "stock_trading_rule",
        type_="check",
    )
    op.drop_constraint(
        "ck_stock_trading_rule_special_limit_nonnegative",
        "stock_trading_rule",
        type_="check",
    )
    op.create_check_constraint(
        "ck_stock_trading_rule_special_limit_nonnegative",
        "stock_trading_rule",
        "special_price_limit_ratio IS NULL OR special_price_limit_ratio >= 0",
    )
    op.drop_constraint(
        "ck_stock_trading_rule_normal_limit_nonnegative",
        "stock_trading_rule",
        type_="check",
    )
    op.create_check_constraint(
        "ck_stock_trading_rule_normal_limit_nonnegative",
        "stock_trading_rule",
        "normal_price_limit_ratio IS NULL OR normal_price_limit_ratio >= 0",
    )
