"""增加统一账户实时风险审计和强平任务基础表。

Revision ID: 20260805_0017
Revises: 20260805_0016
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260805_0017"
down_revision = "20260805_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account",
        sa.Column("risk_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "orders",
        sa.Column("order_source", sa.String(32), server_default="USER", nullable=False),
    )
    op.add_column(
        "orders", sa.Column("liquidation_task_id", sa.String(64), nullable=True)
    )
    op.add_column(
        "orders",
        sa.Column("reduce_only", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_index(
        "ix_orders_liquidation_task_id",
        "orders",
        ["liquidation_task_id"],
        unique=False,
    )

    op.create_table(
        "liquidation_task",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("active_key", sa.String(64), nullable=True),
        sa.Column("trigger_reason", sa.String(128), nullable=False),
        sa.Column("trigger_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_order_id", sa.String(64), nullable=True),
        sa.Column("pending_client_order_id", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_key", name="uq_liquidation_task_active_key"),
        sa.UniqueConstraint("task_id", name="uq_liquidation_task_task_id"),
    )
    op.create_index(
        "ix_liquidation_task_account_id",
        "liquidation_task",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "ix_liquidation_task_status_id",
        "liquidation_task",
        ["status", "id"],
        unique=False,
    )
    op.create_index(
        "ix_liquidation_task_status",
        "liquidation_task",
        ["status"],
        unique=False,
    )

    op.create_table(
        "risk_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("previous_state", sa.String(32), nullable=True),
        sa.Column("risk_state", sa.String(32), nullable=False),
        sa.Column("trigger_reason", sa.String(128), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("business_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "business_version", name="uq_risk_event_account_version"
        ),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index(
        "ix_risk_event_account_id", "risk_event", ["account_id"], unique=False
    )
    op.create_index(
        "ix_risk_event_account_created",
        "risk_event",
        ["account_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_risk_event_account_created", table_name="risk_event")
    op.drop_index("ix_risk_event_account_id", table_name="risk_event")
    op.drop_table("risk_event")
    op.drop_index("ix_liquidation_task_status", table_name="liquidation_task")
    op.drop_index("ix_liquidation_task_status_id", table_name="liquidation_task")
    op.drop_index("ix_liquidation_task_account_id", table_name="liquidation_task")
    op.drop_table("liquidation_task")
    op.drop_index("ix_orders_liquidation_task_id", table_name="orders")
    op.drop_column("orders", "reduce_only")
    op.drop_column("orders", "liquidation_task_id")
    op.drop_column("orders", "order_source")
    op.drop_column("account", "risk_version")
