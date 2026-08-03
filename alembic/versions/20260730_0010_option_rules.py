"""增加期权手续费明细和期权保证金版本规则。

Revision ID: 20260730_0010
Revises: 20260730_0009
"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_0010"
down_revision = "20260730_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "option_margin_rule",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=True),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("instrument_type", sa.String(32), nullable=False),
        sa.Column("margin_algorithm", sa.String(64), nullable=False),
        sa.Column(
            "margin_adjustment_rate", sa.Numeric(24, 12), nullable=False
        ),
        sa.Column(
            "minimum_guarantee_rate", sa.Numeric(24, 12), nullable=False
        ),
        sa.Column(
            "out_of_money_deduction_rate",
            sa.Numeric(24, 12),
            nullable=False,
        ),
        sa.Column(
            "minimum_underlying_margin_ratio",
            sa.Numeric(24, 12),
            nullable=False,
        ),
        sa.Column("extra_margin_rate", sa.Numeric(24, 12), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=False),
        sa.Column("data_source", sa.String(32), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "margin_adjustment_rate >= 0 "
            "AND minimum_guarantee_rate >= 0 "
            "AND out_of_money_deduction_rate >= 0 "
            "AND minimum_underlying_margin_ratio >= 0 "
            "AND extra_margin_rate >= 0",
            name="ck_option_margin_rule_rates_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "exchange_id",
            "product_id",
            "instrument_id",
            "trading_day",
            "rule_version",
            name="uq_option_margin_rule_scope_version",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_option_margin_rule_resolve",
        "option_margin_rule",
        ["exchange_id", "instrument_type", "trading_day", "is_active"],
    )

    op.create_table(
        "fee_rule_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("exchange_id", sa.String(32), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=True),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("instrument_type", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("offset_flag", sa.String(32), nullable=False),
        sa.Column("commission_type", sa.String(32), nullable=False),
        sa.Column(
            "commission_parameter", sa.Numeric(24, 12), nullable=False
        ),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=False),
        sa.Column("data_source", sa.String(32), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "commission_parameter >= 0",
            name="ck_fee_rule_item_parameter_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "exchange_id",
            "product_id",
            "instrument_id",
            "instrument_type",
            "direction",
            "offset_flag",
            "trading_day",
            "rule_version",
            name="uq_fee_rule_item_scope_version",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_fee_rule_item_resolve",
        "fee_rule_item",
        [
            "exchange_id",
            "instrument_type",
            "direction",
            "offset_flag",
            "trading_day",
            "is_active",
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_fee_rule_item_resolve", table_name="fee_rule_item")
    op.drop_table("fee_rule_item")
    op.drop_index(
        "ix_option_margin_rule_resolve",
        table_name="option_margin_rule",
    )
    op.drop_table("option_margin_rule")
