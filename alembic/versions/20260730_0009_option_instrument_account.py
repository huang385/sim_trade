"""扩展期权合约、行情代码映射和统一账户估值字段。

Revision ID: 20260730_0009
Revises: 20260729_0008
"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_0009"
down_revision = "20260729_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 先以可回填默认值增加字段，保证已有纯期货数据库可以直接升级。
    op.add_column(
        "instrument",
        sa.Column(
            "instrument_type",
            sa.String(32),
            nullable=False,
            server_default="FUTURES",
        ),
    )
    op.add_column(
        "instrument",
        sa.Column("underlying_instrument_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "instrument",
        sa.Column("option_type", sa.String(16), nullable=True),
    )
    op.add_column(
        "instrument",
        sa.Column("strike_price", sa.Numeric(24, 6), nullable=True),
    )
    op.add_column(
        "instrument",
        sa.Column("exercise_style", sa.String(16), nullable=True),
    )
    op.add_column(
        "instrument",
        sa.Column("settlement_type", sa.String(16), nullable=True),
    )
    op.add_column(
        "instrument",
        sa.Column("last_trading_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "instrument",
        sa.Column(
            "is_tradeable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.create_foreign_key(
        "fk_instrument_underlying_instrument",
        "instrument",
        "instrument",
        ["underlying_instrument_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_instrument_instrument_type",
        "instrument",
        ["instrument_type"],
    )
    op.create_index(
        "ix_instrument_underlying_instrument_id",
        "instrument",
        ["underlying_instrument_id"],
    )
    op.create_index(
        "ix_instrument_last_trading_date",
        "instrument",
        ["last_trading_date"],
    )
    op.create_index(
        "ix_instrument_is_tradeable",
        "instrument",
        ["is_tradeable"],
    )
    op.create_index(
        "ix_instrument_underlying_type",
        "instrument",
        ["underlying_instrument_id", "instrument_type"],
    )
    op.create_check_constraint(
        "ck_instrument_option_underlying",
        "instrument",
        "instrument_type NOT IN ('FUTURES_OPTION', 'INDEX_OPTION') "
        "OR underlying_instrument_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_instrument_option_type",
        "instrument",
        "instrument_type NOT IN ('FUTURES_OPTION', 'INDEX_OPTION') "
        "OR option_type IN ('CALL', 'PUT')",
    )
    op.create_check_constraint(
        "ck_instrument_option_strike",
        "instrument",
        "instrument_type NOT IN ('FUTURES_OPTION', 'INDEX_OPTION') "
        "OR strike_price > 0",
    )
    op.create_check_constraint(
        "ck_instrument_option_expiry",
        "instrument",
        "instrument_type NOT IN ('FUTURES_OPTION', 'INDEX_OPTION') "
        "OR expire_date IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_instrument_not_self_underlying",
        "instrument",
        "underlying_instrument_id IS NULL OR underlying_instrument_id <> id",
    )
    op.create_check_constraint(
        "ck_instrument_index_not_tradeable",
        "instrument",
        "instrument_type <> 'INDEX' OR is_tradeable = false",
    )
    op.create_check_constraint(
        "ck_instrument_derivative_multiplier_positive",
        "instrument",
        "instrument_type = 'INDEX' OR contract_multiplier > 0",
    )
    # CHECK 约束不能跨行读取标的合约类型，因此用数据库触发器补上
    # FUTURES_OPTION→FUTURES、INDEX_OPTION→INDEX 的强一致性约束。
    op.execute(
        """
        CREATE FUNCTION validate_option_underlying_type()
        RETURNS trigger AS $$
        DECLARE
            underlying_type varchar(32);
        BEGIN
            IF NEW.instrument_type IN ('FUTURES_OPTION', 'INDEX_OPTION') THEN
                SELECT instrument_type INTO underlying_type
                FROM instrument
                WHERE id = NEW.underlying_instrument_id;
                IF underlying_type IS NULL THEN
                    RAISE EXCEPTION 'option underlying instrument not found';
                END IF;
                IF NEW.instrument_type = 'FUTURES_OPTION'
                   AND underlying_type <> 'FUTURES' THEN
                    RAISE EXCEPTION
                        'FUTURES_OPTION underlying must be FUTURES';
                END IF;
                IF NEW.instrument_type = 'INDEX_OPTION'
                   AND underlying_type <> 'INDEX' THEN
                    RAISE EXCEPTION
                        'INDEX_OPTION underlying must be INDEX';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_validate_option_underlying_type
        BEFORE INSERT OR UPDATE OF instrument_type, underlying_instrument_id
        ON instrument
        FOR EACH ROW EXECUTE FUNCTION validate_option_underlying_type()
        """
    )

    op.create_table(
        "instrument_market_data_mapping",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("data_source", sa.String(32), nullable=False),
        sa.Column("market_data_code", sa.String(128), nullable=False),
        sa.Column(
            "market_data_type",
            sa.String(32),
            nullable=False,
            server_default="TICK",
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "data_source",
            "market_data_code",
            name="uq_market_mapping_source_code",
        ),
        sa.UniqueConstraint(
            "instrument_id",
            "data_source",
            name="uq_market_mapping_instrument_source",
        ),
    )
    op.create_index(
        "ix_instrument_market_data_mapping_instrument_id",
        "instrument_market_data_mapping",
        ["instrument_id"],
    )
    op.create_index(
        "ix_market_mapping_instrument_enabled",
        "instrument_market_data_mapping",
        ["instrument_id", "is_enabled"],
    )

    account_columns = (
        sa.Column(
            "option_trading_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "option_used_margin",
            sa.Numeric(24, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "option_realtime_required_margin",
            sa.Numeric(24, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "long_option_market_value",
            sa.Numeric(24, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "short_option_market_value",
            sa.Numeric(24, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "net_option_market_value",
            sa.Numeric(24, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "risk_available_cash",
            sa.Numeric(24, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "risk_state",
            sa.String(32),
            nullable=False,
            server_default="NORMAL",
        ),
    )
    for column in account_columns:
        op.add_column("account", column)
    op.execute("UPDATE account SET risk_available_cash = available_cash")
    op.create_check_constraint(
        "ck_account_option_used_margin_nonnegative",
        "account",
        "option_used_margin >= 0",
    )
    op.create_check_constraint(
        "ck_account_option_realtime_margin_nonnegative",
        "account",
        "option_realtime_required_margin >= 0",
    )
    op.create_check_constraint(
        "ck_account_long_option_value_nonnegative",
        "account",
        "long_option_market_value >= 0",
    )
    op.create_check_constraint(
        "ck_account_short_option_value_nonnegative",
        "account",
        "short_option_market_value >= 0",
    )


def downgrade() -> None:
    for name in (
        "ck_account_short_option_value_nonnegative",
        "ck_account_long_option_value_nonnegative",
        "ck_account_option_realtime_margin_nonnegative",
        "ck_account_option_used_margin_nonnegative",
    ):
        op.drop_constraint(name, "account", type_="check")
    for column in (
        "risk_state",
        "risk_available_cash",
        "net_option_market_value",
        "short_option_market_value",
        "long_option_market_value",
        "option_realtime_required_margin",
        "option_used_margin",
        "option_trading_enabled",
    ):
        op.drop_column("account", column)

    op.drop_index(
        "ix_market_mapping_instrument_enabled",
        table_name="instrument_market_data_mapping",
    )
    op.drop_index(
        "ix_instrument_market_data_mapping_instrument_id",
        table_name="instrument_market_data_mapping",
    )
    op.drop_table("instrument_market_data_mapping")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_validate_option_underlying_type "
        "ON instrument"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS validate_option_underlying_type()"
    )

    for name in (
        "ck_instrument_derivative_multiplier_positive",
        "ck_instrument_index_not_tradeable",
        "ck_instrument_not_self_underlying",
        "ck_instrument_option_expiry",
        "ck_instrument_option_strike",
        "ck_instrument_option_type",
        "ck_instrument_option_underlying",
    ):
        op.drop_constraint(name, "instrument", type_="check")
    for name in (
        "ix_instrument_underlying_type",
        "ix_instrument_is_tradeable",
        "ix_instrument_last_trading_date",
        "ix_instrument_underlying_instrument_id",
        "ix_instrument_instrument_type",
    ):
        op.drop_index(name, table_name="instrument")
    op.drop_constraint(
        "fk_instrument_underlying_instrument",
        "instrument",
        type_="foreignkey",
    )
    for column in (
        "is_tradeable",
        "last_trading_date",
        "settlement_type",
        "exercise_style",
        "strike_price",
        "option_type",
        "underlying_instrument_id",
        "instrument_type",
    ):
        op.drop_column("instrument", column)
