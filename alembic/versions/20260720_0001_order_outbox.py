"""创建订单表和事务 Outbox 事件表

Revision ID: 20260720_0001
Revises:
Create Date: 2026-07-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260720_0001"
down_revision: Union[str, None] = "20260719_0000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_names() -> set[str]:
    """读取当前表名，以兼容开发库中历史 create_all 创建的 orders。"""

    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table_name: str) -> set[str]:
    """读取指定表的索引名。"""

    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes(table_name)
        if item.get("name")
    }


def upgrade() -> None:
    tables = _table_names()

    if "orders" not in tables:
        op.create_table(
            "orders",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("order_id", sa.String(64), nullable=False),
            sa.Column("client_order_id", sa.String(64), nullable=False),
            sa.Column("account_id", sa.String(64), nullable=False),
            sa.Column("order_book_id", sa.String(64), nullable=False),
            sa.Column("symbol", sa.String(64), nullable=False),
            sa.Column("exchange_id", sa.String(32), nullable=False),
            sa.Column("trading_day", sa.Date(), nullable=False),
            sa.Column("direction", sa.String(16), nullable=False),
            sa.Column("offset_flag", sa.String(32), nullable=False),
            sa.Column("order_type", sa.String(16), nullable=False),
            sa.Column("limit_price", sa.Numeric(24, 6), nullable=False),
            sa.Column("total_volume", sa.Integer(), nullable=False),
            sa.Column("traded_volume", sa.Integer(), nullable=False),
            sa.Column("remaining_volume", sa.Integer(), nullable=False),
            sa.Column("average_price", sa.Numeric(24, 6), nullable=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("submit_status", sa.String(32), nullable=False),
            sa.Column("frozen_margin", sa.Numeric(24, 6), nullable=False),
            sa.Column("frozen_commission", sa.Numeric(24, 6), nullable=False),
            sa.Column("frozen_position_volume", sa.Integer(), nullable=False),
            sa.Column("reject_code", sa.String(64), nullable=True),
            sa.Column("reject_message", sa.String(256), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.Column(
                "accepted_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.PrimaryKeyConstraint("id", name="pk_orders"),
            sa.UniqueConstraint("order_id", name="uq_order_order_id"),
            sa.UniqueConstraint(
                "account_id",
                "client_order_id",
                name="uq_order_account_client_order_id",
            ),
        )
    else:
        # 早期开发版本由 create_all 建表，保留数据并补齐本次结构差异。
        op.alter_column(
            "orders",
            "average_price",
            existing_type=sa.Numeric(24, 6),
            nullable=True,
        )
        inspector = sa.inspect(op.get_bind())
        unique_names = {
            item["name"]
            for item in inspector.get_unique_constraints("orders")
            if item.get("name")
        }
        existing_indexes = {
            item["name"]: item
            for item in inspector.get_indexes("orders")
            if item.get("name")
        }
        if "uq_order_order_id" not in unique_names:
            legacy_index = existing_indexes.get("ix_orders_order_id")
            if legacy_index and legacy_index.get("unique"):
                # 把旧的唯一索引原地提升为命名唯一约束，不重建数据。
                op.execute(
                    "ALTER INDEX ix_orders_order_id "
                    "RENAME TO uq_order_order_id"
                )
                op.execute(
                    "ALTER TABLE orders ADD CONSTRAINT uq_order_order_id "
                    "UNIQUE USING INDEX uq_order_order_id"
                )
            else:
                op.create_unique_constraint(
                    "uq_order_order_id", "orders", ["order_id"]
                )

    order_indexes = _index_names("orders")
    for name, columns in (
        ("ix_orders_account_id", ["account_id"]),
        ("ix_orders_order_book_id", ["order_book_id"]),
        ("ix_orders_symbol", ["symbol"]),
        ("ix_orders_exchange_id", ["exchange_id"]),
        ("ix_orders_trading_day", ["trading_day"]),
        ("ix_orders_status", ["status"]),
        ("ix_order_exchange_symbol", ["exchange_id", "symbol"]),
        ("ix_order_created_at", ["created_at"]),
    ):
        if name not in order_indexes:
            op.create_index(name, "orders", columns, unique=False)

    if "outbox_event" not in tables:
        op.create_table(
            "outbox_event",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("event_id", sa.String(64), nullable=False),
            sa.Column("aggregate_type", sa.String(32), nullable=False),
            sa.Column("aggregate_id", sa.String(64), nullable=False),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column(
                "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
            ),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("retry_count", sa.Integer(), nullable=False),
            sa.Column("max_retries", sa.Integer(), nullable=False),
            sa.Column(
                "next_retry_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.PrimaryKeyConstraint("id", name="pk_outbox_event"),
            sa.UniqueConstraint(
                "event_id", name="uq_outbox_event_event_id"
            ),
        )

    outbox_indexes = _index_names("outbox_event")
    if "ix_outbox_event_pending_scan" not in outbox_indexes:
        op.create_index(
            "ix_outbox_event_pending_scan",
            "outbox_event",
            ["status", "next_retry_at", "id"],
            unique=False,
        )
    if "ix_outbox_event_aggregate" not in outbox_indexes:
        op.create_index(
            "ix_outbox_event_aggregate",
            "outbox_event",
            ["aggregate_type", "aggregate_id"],
            unique=False,
        )


def downgrade() -> None:
    """
    只回退本阶段新增的 Outbox 表和订单辅助索引。

    orders 可能来自已有开发库并含有用户数据，因此此迁移不会删除订单表。
    """

    tables = _table_names()
    if "outbox_event" in tables:
        op.drop_table("outbox_event")
    if "orders" in tables:
        indexes = _index_names("orders")
        if "ix_order_created_at" in indexes:
            op.drop_index("ix_order_created_at", table_name="orders")
        if "ix_order_exchange_symbol" in indexes:
            op.drop_index("ix_order_exchange_symbol", table_name="orders")
