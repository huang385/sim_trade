"""Add the shared data foundation for future cash-stock trading.

Revision ID: 20260817_0023
Revises: 20260814_0022
"""

import sqlalchemy as sa
from alembic import op


revision = "20260817_0023"
down_revision = "20260814_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account",
        sa.Column(
            "stock_market_value",
            sa.Numeric(24, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.execute(
        "UPDATE account SET stock_market_value = 0 "
        "WHERE stock_market_value IS NULL"
    )
    op.create_check_constraint(
        "ck_account_stock_market_value_nonnegative",
        "account",
        "stock_market_value >= 0",
    )

    op.add_column(
        "position",
        sa.Column(
            "settlement_locked_volume",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.execute(
        "UPDATE position SET settlement_locked_volume = 0 "
        "WHERE settlement_locked_volume IS NULL"
    )
    op.create_check_constraint(
        "ck_position_settlement_locked_nonnegative",
        "position",
        "settlement_locked_volume >= 0",
    )
    op.drop_constraint(
        "ck_position_available_balance", "position", type_="check"
    )
    op.create_check_constraint(
        "ck_position_available_balance",
        "position",
        "available_volume = total_volume - frozen_volume - settlement_locked_volume",
    )
    op.create_check_constraint(
        "ck_position_reserved_volume_within_total",
        "position",
        "frozen_volume + settlement_locked_volume <= total_volume",
    )

    op.alter_column("orders", "offset_flag", nullable=True)
    op.create_check_constraint(
        "ck_order_stock_offset_flag_semantics",
        "orders",
        "(instrument_type = 'STOCK' AND offset_flag IS NULL) OR "
        "(instrument_type <> 'STOCK' AND offset_flag IS NOT NULL)",
    )
    op.alter_column("trade", "offset_flag", nullable=True)
    op.create_check_constraint(
        "ck_trade_stock_offset_flag_semantics",
        "trade",
        "(instrument_type = 'STOCK' AND offset_flag IS NULL) OR "
        "(instrument_type <> 'STOCK' AND offset_flag IS NOT NULL)",
    )

    op.create_check_constraint(
        "ck_instrument_stock_market_type",
        "instrument",
        "instrument_type <> 'STOCK' OR market_type = 'STOCK'",
    )
    op.create_check_constraint(
        "ck_instrument_stock_multiplier_one",
        "instrument",
        "instrument_type <> 'STOCK' OR contract_multiplier = 1",
    )
    op.create_check_constraint(
        "ck_instrument_stock_option_fields_empty",
        "instrument",
        "instrument_type <> 'STOCK' OR ("
        "underlying_instrument_id IS NULL AND option_type IS NULL AND "
        "strike_price IS NULL AND exercise_style IS NULL AND "
        "settlement_type IS NULL)",
    )

    op.create_table(
        "stock_trading_rule",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("buy_lot_size", sa.Integer(), nullable=False),
        sa.Column(
            "buy_volume_must_be_multiple", sa.Boolean(), nullable=False
        ),
        sa.Column("sell_min_unit", sa.Integer(), nullable=False),
        sa.Column("sell_odd_lot_allowed", sa.Boolean(), nullable=False),
        sa.Column("settlement_days", sa.Integer(), nullable=False),
        sa.Column("price_limit_type", sa.String(32), nullable=False),
        sa.Column(
            "normal_price_limit_ratio", sa.Numeric(18, 8), nullable=True
        ),
        sa.Column(
            "special_price_limit_ratio", sa.Numeric(18, 8), nullable=True
        ),
        sa.Column("price_cage_enabled", sa.Boolean(), nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("data_source", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "buy_lot_size > 0 AND sell_min_unit > 0 AND settlement_days >= 0",
            name="ck_stock_trading_rule_volume_and_settlement_valid",
        ),
        sa.CheckConstraint(
            "normal_price_limit_ratio IS NULL OR normal_price_limit_ratio >= 0",
            name="ck_stock_trading_rule_normal_limit_nonnegative",
        ),
        sa.CheckConstraint(
            "special_price_limit_ratio IS NULL OR special_price_limit_ratio >= 0",
            name="ck_stock_trading_rule_special_limit_nonnegative",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_stock_trading_rule_effective_period_valid",
        ),
        sa.CheckConstraint(
            "rule_version <> '' AND data_source <> ''",
            name="ck_stock_trading_rule_identity_not_empty",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id",
            "rule_version",
            name="uq_stock_trading_rule_instrument_version",
        ),
    )
    op.create_index(
        "ix_stock_trading_rule_instrument_id",
        "stock_trading_rule",
        ["instrument_id"],
    )
    op.create_index(
        "ix_stock_trading_rule_instrument_effective",
        "stock_trading_rule",
        ["instrument_id", "effective_from", "effective_to"],
    )

    op.create_table(
        "stock_daily_trading_fact",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("previous_close", sa.Numeric(24, 6), nullable=False),
        sa.Column("upper_limit_price", sa.Numeric(24, 6), nullable=True),
        sa.Column("lower_limit_price", sa.Numeric(24, 6), nullable=True),
        sa.Column("is_suspended", sa.Boolean(), nullable=False),
        sa.Column("is_special_treatment", sa.Boolean(), nullable=False),
        sa.Column("is_tradeable", sa.Boolean(), nullable=False),
        sa.Column("source_event_id", sa.String(128), nullable=False),
        sa.Column("data_source", sa.String(32), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "previous_close > 0",
            name="ck_stock_daily_trading_fact_previous_close_positive",
        ),
        sa.CheckConstraint(
            "(upper_limit_price IS NULL AND lower_limit_price IS NULL) OR "
            "(upper_limit_price > 0 AND lower_limit_price > 0)",
            name="ck_stock_daily_trading_fact_limit_prices_valid",
        ),
        sa.CheckConstraint(
            "source_event_id <> '' AND data_source <> ''",
            name="ck_stock_daily_trading_fact_identity_not_empty",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id",
            "trading_day",
            name="uq_stock_daily_trading_fact_instrument_day",
        ),
    )
    op.create_index(
        "ix_stock_daily_trading_fact_instrument_id",
        "stock_daily_trading_fact",
        ["instrument_id"],
    )
    op.create_index(
        "ix_stock_daily_trading_fact_trading_day",
        "stock_daily_trading_fact",
        ["trading_day"],
    )
    op.create_index(
        "ix_stock_daily_trading_fact_instrument_day_lookup",
        "stock_daily_trading_fact",
        ["instrument_id", "trading_day"],
    )


def downgrade() -> None:
    # 股票事实一旦写入，旧版本既不能解释其数据，也不能安全恢复旧的数量约束。
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM stock_daily_trading_fact) "
        "OR EXISTS (SELECT 1 FROM stock_trading_rule) "
        "OR EXISTS (SELECT 1 FROM instrument WHERE instrument_type = 'STOCK') "
        "OR EXISTS (SELECT 1 FROM orders WHERE instrument_type = 'STOCK') "
        "OR EXISTS (SELECT 1 FROM trade WHERE instrument_type = 'STOCK') "
        "OR EXISTS (SELECT 1 FROM account WHERE stock_market_value <> 0) "
        "OR EXISTS (SELECT 1 FROM position WHERE settlement_locked_volume <> 0) "
        "THEN RAISE EXCEPTION 'stock data foundation contains data; downgrade is unsafe'; "
        "END IF; END $$;"
    )
    op.drop_table("stock_daily_trading_fact")
    op.drop_table("stock_trading_rule")

    op.drop_constraint(
        "ck_instrument_stock_option_fields_empty", "instrument", type_="check"
    )
    op.drop_constraint(
        "ck_instrument_stock_multiplier_one", "instrument", type_="check"
    )
    op.drop_constraint(
        "ck_instrument_stock_market_type", "instrument", type_="check"
    )

    op.drop_constraint(
        "ck_trade_stock_offset_flag_semantics", "trade", type_="check"
    )
    op.alter_column("trade", "offset_flag", nullable=False)
    op.drop_constraint(
        "ck_order_stock_offset_flag_semantics", "orders", type_="check"
    )
    op.alter_column("orders", "offset_flag", nullable=False)

    op.drop_constraint(
        "ck_position_reserved_volume_within_total", "position", type_="check"
    )
    op.drop_constraint("ck_position_available_balance", "position", type_="check")
    op.create_check_constraint(
        "ck_position_available_balance",
        "position",
        "available_volume = total_volume - frozen_volume",
    )
    op.drop_constraint(
        "ck_position_settlement_locked_nonnegative", "position", type_="check"
    )
    op.drop_column("position", "settlement_locked_volume")

    op.drop_constraint(
        "ck_account_stock_market_value_nonnegative", "account", type_="check"
    )
    op.drop_column("account", "stock_market_value")
