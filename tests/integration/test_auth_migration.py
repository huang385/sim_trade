"""认证阶段Alembic迁移的真实PostgreSQL验证。"""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from app.core.config import settings


pytestmark = pytest.mark.integration


def _admin_dsn(*, database: str) -> str:
    """使用项目数据库配置连接指定数据库，不在测试中硬编码口令。"""

    return (
        f"host={settings.postgres_host} port={settings.postgres_port} "
        f"dbname={database} user={settings.postgres_user} "
        f"password={settings.postgres_password}"
    )


@contextmanager
def _temporary_database():
    """
    创建隔离的临时数据库。

    只有数据库确实不可连接或测试用户没有建库权限时才跳过；迁移或业务断言
    失败不会被宽泛捕获，确保真实回归能够正常暴露。
    """

    database_name = f"sim_trade_auth_test_{uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(
            _admin_dsn(database="postgres"),
            autocommit=True,
        )
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL不可连接，跳过迁移集成测试: {exc}")

    try:
        try:
            admin.execute(
                sql.SQL("CREATE DATABASE {}").format(
                    sql.Identifier(database_name)
                )
            )
        except psycopg.errors.InsufficientPrivilege as exc:
            pytest.skip(f"数据库用户没有CREATE DATABASE权限: {exc}")
        yield database_name
    finally:
        admin.execute(
            "SELECT pg_terminate_backend(pid) "
            "FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (database_name,),
        )
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(
                sql.Identifier(database_name)
            )
        )
        admin.close()


def _run_alembic(database_name: str, *arguments: str) -> None:
    """在临时库中运行Alembic，失败时保留完整输出给pytest。"""

    environment = os.environ.copy()
    environment["DEBUG"] = "false"
    environment["POSTGRES_DB"] = database_name
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=os.getcwd(),
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )


def test_empty_database_can_upgrade_to_head_and_downgrade_to_base():
    """全新环境可以从Base升级到Head，并有可执行的完整降级路径。"""

    with _temporary_database() as database_name:
        _run_alembic(database_name, "upgrade", "head")
        _run_alembic(database_name, "check")
        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            revision = db.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            assert revision == "20260729_0008"
            nullable = db.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND table_name = 'account' AND column_name = 'user_id'"
            ).fetchone()[0]
            assert nullable == "NO"

        _run_alembic(database_name, "downgrade", "base")
        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            remaining_core_tables = db.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name IN "
                "('account', 'app_user', 'auth_refresh_session')"
            ).fetchone()[0]
            assert remaining_core_tables == 0


def test_existing_accounts_are_safely_backfilled_before_foreign_key():
    """历史账户先建立禁用占位用户，空归属也会回填后再设为非空。"""

    with _temporary_database() as database_name:
        _run_alembic(database_name, "upgrade", "20260727_0007")
        now = datetime.now(timezone.utc)
        account_columns = (
            "account_id, user_id, account_name, account_type, "
            "initial_cash, cash_balance, available_cash, frozen_cash, "
            "equity, used_margin, frozen_margin, realized_pnl, "
            "unrealized_pnl, daily_position_pnl, daily_close_pnl, "
            "daily_commission, daily_pnl, used_commission, "
            "frozen_commission, risk_ratio, status, trading_day, "
            "created_at, updated_at"
        )
        values = (
            "%s, %s, %s, 'FUTURES', "
            "100000, 100000, 100000, 0, 100000, 0, 0, 0, 0, 0, 0, "
            "0, 0, 0, 0, 0, 'NORMAL', NULL, %s, %s"
        )
        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            db.execute(
                f"INSERT INTO account ({account_columns}) VALUES ({values})",
                (
                    "LEGACY-A001",
                    "LEGACY_USER",
                    "历史归属账户",
                    now,
                    now,
                ),
            )
            db.execute(
                f"INSERT INTO account ({account_columns}) VALUES ({values})",
                (
                    "LEGACY-A002",
                    None,
                    "历史空归属账户",
                    now,
                    now,
                ),
            )
            db.commit()

        _run_alembic(database_name, "upgrade", "head")
        _run_alembic(database_name, "check")
        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            rows = db.execute(
                "SELECT account.account_id, account.user_id, app_user.status "
                "FROM account JOIN app_user "
                "ON app_user.user_id = account.user_id "
                "ORDER BY account.account_id"
            ).fetchall()
            assert rows == [
                ("LEGACY-A001", "LEGACY_USER", "DISABLED"),
                (
                    "LEGACY-A002",
                    "U_MIGRATION_OWNER",
                    "DISABLED",
                ),
            ]
            invalid_owner_count = db.execute(
                "SELECT count(*) FROM account "
                "LEFT JOIN app_user "
                "ON app_user.user_id = account.user_id "
                "WHERE account.user_id IS NULL OR app_user.id IS NULL"
            ).fetchone()[0]
            assert invalid_owner_count == 0
