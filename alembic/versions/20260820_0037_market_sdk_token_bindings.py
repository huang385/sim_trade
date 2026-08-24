"""按客户端IP绑定行情SDK凭证

Revision ID: 20260820_0037
Revises: 20260819_0036
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260820_0037"
down_revision: Union[str, None] = "20260819_0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """建立IP→实时/数据库行情SDK token绑定表。"""

    op.create_table(
        "market_sdk_token_binding",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("client_ip", sa.String(64), nullable=False),
        sa.Column("live_sdk_token", sa.Text(), nullable=False),
        sa.Column("data_sdk_token", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False, server_default="lan"),
        sa.Column("live_server_url", sa.String(256), nullable=True),
        sa.Column("data_server_url", sa.String(256), nullable=True),
        sa.Column("remark", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_market_sdk_token_binding"),
        sa.UniqueConstraint(
            "client_ip", name="uq_market_sdk_token_binding_client_ip"
        ),
    )
    op.create_index(
        "ix_market_sdk_token_binding_client_ip",
        "market_sdk_token_binding",
        ["client_ip"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_sdk_token_binding_client_ip",
        table_name="market_sdk_token_binding",
    )
    op.drop_table("market_sdk_token_binding")
