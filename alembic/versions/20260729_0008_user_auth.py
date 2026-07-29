"""增加用户认证、Refresh会话和交易账户归属约束。

Revision ID: 20260729_0008
Revises: 20260727_0007
Create Date: 2026-07-29
"""

from datetime import datetime, timezone
import hashlib
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260729_0008"
down_revision: Union[str, None] = "20260727_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MIGRATION_OWNER_ID = "U_MIGRATION_OWNER"


def _placeholder_username(user_id: str) -> str:
    """生成不包含原始用户编号、不会冲突的迁移占位登录名。"""

    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]
    return f"migrated_{digest}"


def upgrade() -> None:
    """先回填旧账户归属，再建立非空和外键约束。"""

    op.create_table(
        "app_user",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "failed_login_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "locked_until", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_login_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_app_user_user_id"),
        sa.UniqueConstraint("username", name="uq_app_user_username"),
    )
    op.create_index("ix_app_user_user_id", "app_user", ["user_id"])
    op.create_index("ix_app_user_username", "app_user", ["username"])

    connection = op.get_bind()
    existing_user_ids = [
        str(row[0]).strip()
        for row in connection.execute(
            sa.text(
                "SELECT DISTINCT user_id FROM account "
                "WHERE user_id IS NOT NULL AND btrim(user_id) <> ''"
            )
        )
    ]
    if connection.execute(
        sa.text(
            "SELECT count(*) FROM account "
            "WHERE user_id IS NULL OR btrim(user_id) = ''"
        )
    ).scalar_one():
        existing_user_ids.append(MIGRATION_OWNER_ID)

    now = datetime.now(timezone.utc)
    user_table = sa.table(
        "app_user",
        sa.column("user_id", sa.String),
        sa.column("username", sa.String),
        sa.column("password_hash", sa.String),
        sa.column("display_name", sa.String),
        sa.column("role", sa.String),
        sa.column("status", sa.String),
        sa.column("failed_login_count", sa.Integer),
        sa.column("password_changed_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    if existing_user_ids:
        op.bulk_insert(
            user_table,
            [
                {
                    "user_id": user_id,
                    "username": _placeholder_username(user_id),
                    # 占位用户始终DISABLED，该值故意不是有效Argon2哈希。
                    "password_hash": "!migration-placeholder-no-login!",
                    "display_name": "历史账户迁移占位用户",
                    "role": "USER",
                    "status": "DISABLED",
                    "failed_login_count": 0,
                    "password_changed_at": now,
                    "created_at": now,
                    "updated_at": now,
                }
                for user_id in sorted(set(existing_user_ids))
            ],
        )

    connection.execute(
        sa.text(
            "UPDATE account SET user_id = :owner "
            "WHERE user_id IS NULL OR btrim(user_id) = ''"
        ),
        {"owner": MIGRATION_OWNER_ID},
    )
    op.alter_column(
        "account",
        "user_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_account_user_id_app_user",
        "account",
        "app_user",
        ["user_id"],
        ["user_id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "auth_refresh_session",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "revoked_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "replaced_by_jti", sa.String(length=64), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "last_used_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("client_ip", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.user_id"],
            name="fk_auth_refresh_session_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "jti", name="uq_auth_refresh_session_jti"
        ),
    )
    op.create_index(
        "ix_auth_refresh_session_jti",
        "auth_refresh_session",
        ["jti"],
    )
    op.create_index(
        "ix_auth_refresh_session_user_id",
        "auth_refresh_session",
        ["user_id"],
    )
    op.create_index(
        "ix_auth_refresh_session_expires_at",
        "auth_refresh_session",
        ["expires_at"],
    )


def downgrade() -> None:
    """移除认证表；保留Account.user_id数据但恢复为可空。"""

    op.drop_index(
        "ix_auth_refresh_session_expires_at",
        table_name="auth_refresh_session",
    )
    op.drop_index(
        "ix_auth_refresh_session_user_id",
        table_name="auth_refresh_session",
    )
    op.drop_index(
        "ix_auth_refresh_session_jti",
        table_name="auth_refresh_session",
    )
    op.drop_table("auth_refresh_session")
    op.drop_constraint(
        "fk_account_user_id_app_user",
        "account",
        type_="foreignkey",
    )
    op.alter_column(
        "account",
        "user_id",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.drop_index("ix_app_user_username", table_name="app_user")
    op.drop_index("ix_app_user_user_id", table_name="app_user")
    op.drop_table("app_user")
