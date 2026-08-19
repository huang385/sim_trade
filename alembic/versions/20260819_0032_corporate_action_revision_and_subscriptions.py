"""Protect corporate-action revisions and support partial rights subscriptions.

Revision ID: 20260819_0032
Revises: 20260818_0031
"""

import sqlalchemy as sa
from alembic import op


revision = "20260819_0032"
down_revision = "20260818_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cash_security_corporate_action",
        sa.Column("superseded_by_action_id", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_cash_corporate_action_superseded_by",
        "cash_security_corporate_action",
        "cash_security_corporate_action",
        ["superseded_by_action_id"],
        ["action_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_cash_security_corporate_action_source_version",
        "cash_security_corporate_action",
        ["source_action_id", "action_version"],
    )
    op.create_table(
        "cash_security_corporate_action_subscription",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "entitlement_id",
            sa.String(64),
            sa.ForeignKey("cash_security_corporate_action_entitlement.entitlement_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "action_id",
            sa.String(64),
            sa.ForeignKey("cash_security_corporate_action.action_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.String(64),
            sa.ForeignKey("account.account_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("client_request_id", sa.String(128), nullable=False),
        sa.Column("volume", sa.Integer(), nullable=False),
        sa.Column("cash_amount", sa.Numeric(24, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("entitlement_id", "client_request_id", name="uq_cash_corporate_subscription_request"),
        sa.CheckConstraint("volume > 0 AND cash_amount >= 0", name="ck_cash_corporate_subscription_amounts"),
    )
    op.create_index("ix_cash_security_corporate_action_subscription_entitlement_id", "cash_security_corporate_action_subscription", ["entitlement_id"])
    op.create_index("ix_cash_security_corporate_action_subscription_action_id", "cash_security_corporate_action_subscription", ["action_id"])
    op.create_index("ix_cash_security_corporate_action_subscription_account_id", "cash_security_corporate_action_subscription", ["account_id"])


def downgrade() -> None:
    raise RuntimeError("Corporate-action revision and subscription facts are intentionally irreversible")
