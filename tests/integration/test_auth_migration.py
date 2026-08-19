"""认证阶段Alembic迁移的真实PostgreSQL验证。"""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
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


def test_empty_database_upgrades_to_head_and_rejects_irreversible_downgrade():
    """空库可升级；含资金历史的新Head明确拒绝自动删除事实。"""

    with _temporary_database() as database_name:
        _run_alembic(database_name, "upgrade", "head")
        _run_alembic(database_name, "check")
        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            revision = db.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            # 公司行为、权益和除权除息事实是当前 Head。
            assert revision == "20260819_0036"
            nullable = db.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND table_name = 'account' AND column_name = 'user_id'"
            ).fetchone()[0]
            assert nullable == "NO"

        with pytest.raises(subprocess.CalledProcessError):
            _run_alembic(database_name, "downgrade", "base")
        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            revision = db.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            settlement_tables = db.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name IN "
                "('daily_settlement_batch', 'instrument_settlement_price', "
                "'daily_account_settlement', 'daily_position_settlement', "
                "'option_expiry_settlement_detail')"
            ).fetchone()[0]
            # PostgreSQL wraps the downgrade chain in one transaction.  The
            # The irreversible corporate-action boundary rolls the entire
            # downgrade chain back, so the database remains at the head.
            assert revision == "20260819_0036"
            assert settlement_tables == 5


def test_option_migrations_enforce_underlying_type_and_rule_scope():
    """数据库自身拒绝错误标的类型，并对NULL范围规则执行真正唯一约束。"""

    with _temporary_database() as database_name:
        _run_alembic(database_name, "upgrade", "head")
        now = datetime.now(timezone.utc)
        insert_instrument = (
            "INSERT INTO instrument ("
            "order_book_id, symbol, exchange_id, market_type, "
            "instrument_type, underlying_instrument_id, option_type, "
            "strike_price, contract_multiplier, price_tick, min_volume, "
            "max_volume, expire_date, is_active, is_tradeable, data_source, "
            "created_at, updated_at"
            ") VALUES ("
            "%s, %s, %s, 'FUTURES', %s, %s, %s, %s, 10, 1, 1, 100, "
            "'2026-09-30', true, %s, 'TEST', %s, %s"
            ") RETURNING id"
        )
        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            futures_id = db.execute(
                insert_instrument,
                (
                    "AG2609",
                    "AG2609",
                    "SHFE",
                    "FUTURES",
                    None,
                    None,
                    None,
                    True,
                    now,
                    now,
                ),
            ).fetchone()[0]
            db.commit()

        with (
            psycopg.connect(_admin_dsn(database=database_name)) as db,
            pytest.raises(psycopg.errors.RaiseException),
        ):
            db.execute(
                insert_instrument,
                (
                    "IO2609-C-4000",
                    "IO2609-C-4000",
                    "CFFEX",
                    "INDEX_OPTION",
                    futures_id,
                    "CALL",
                    4000,
                    True,
                    now,
                    now,
                ),
            )

        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            nulls_not_distinct = db.execute(
                "SELECT bool_and(i.indnullsnotdistinct) "
                "FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname IN "
                "('uq_option_margin_rule_scope_version', "
                "'uq_fee_rule_item_scope_version')"
            ).fetchone()[0]
            assert nulls_not_distinct is True


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


def test_historical_futures_multipliers_are_repaired_from_instruments():
    """0011遗留的NULL/固定1乘数必须按每个真实合约分别修复。"""

    with _temporary_database() as database_name:
        _run_alembic(database_name, "upgrade", "20260730_0011")
        now = datetime.now(timezone.utc)
        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            for index, multiplier in enumerate((15, 10), start=1):
                code = f"LEGACY{index}"
                db.execute(
                    "INSERT INTO instrument ("
                    "order_book_id, symbol, exchange_id, market_type, "
                    "instrument_type, contract_multiplier, price_tick, "
                    "min_volume, max_volume, is_active, is_tradeable, "
                    "data_source, created_at, updated_at"
                    ") VALUES (%s, %s, 'TEST', 'FUTURES', 'FUTURES', "
                    "%s, 1, 1, 100, true, true, 'TEST', %s, %s)",
                    (code, code, multiplier, now, now),
                )
                db.execute(
                    "INSERT INTO position ("
                    "position_id, account_id, order_book_id, exchange_id, "
                    "symbol, direction, total_volume, today_volume, "
                    "yesterday_volume, frozen_volume, available_volume, "
                    "average_open_price, position_cost, used_margin, "
                    "initial_occupied_margin, realtime_required_margin, "
                    "multiplier_snapshot, realized_pnl, unrealized_pnl, "
                    "daily_position_pnl, daily_close_pnl, trading_day, "
                    "instrument_type, created_at, updated_at"
                    ") VALUES ("
                    "%s, 'A-LEGACY', %s, 'TEST', %s, 'LONG', "
                    "1, 1, 0, 0, 1, 100, %s, 100, 100, 100, NULL, "
                    "0, 0, 0, 0, CURRENT_DATE, 'FUTURES', %s, %s)",
                    (
                        f"P-{index}",
                        code,
                        code,
                        Decimal("100") * multiplier,
                        now,
                        now,
                    ),
                )
                db.execute(
                    "INSERT INTO position_detail ("
                    "position_detail_id, position_id, account_id, "
                    "open_trade_id, order_book_id, exchange_id, symbol, "
                    "direction, open_trading_day, open_price, "
                    "pnl_base_price, original_volume, remaining_volume, "
                    "frozen_volume, open_margin, remaining_margin, "
                    "initial_occupied_margin, realtime_required_margin, "
                    "multiplier_snapshot, open_commission, status, "
                    "instrument_type, created_at, updated_at"
                    ") VALUES ("
                    "%s, %s, 'A-LEGACY', %s, %s, 'TEST', %s, 'LONG', "
                    "CURRENT_DATE, 100, 100, 1, 1, 0, 100, 100, 100, "
                    "100, 1, 0, 'OPEN', 'FUTURES', %s, %s)",
                    (
                        f"PD-{index}",
                        f"P-{index}",
                        f"T-{index}",
                        code,
                        code,
                        now,
                        now,
                    ),
                )
            db.commit()

        _run_alembic(database_name, "upgrade", "head")
        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            positions = db.execute(
                "SELECT position_id, multiplier_snapshot FROM position "
                "ORDER BY position_id"
            ).fetchall()
            details = db.execute(
                "SELECT position_detail_id, multiplier_snapshot "
                "FROM position_detail ORDER BY position_detail_id"
            ).fetchall()

        assert positions == [("P-1", Decimal("15")), ("P-2", Decimal("10"))]
        assert details == [("PD-1", Decimal("15")), ("PD-2", Decimal("10"))]


def test_untraceable_historical_detail_stops_multiplier_migration():
    """找不到Instrument且没有成交事实时，禁止把0011默认值1当成真值。"""

    with _temporary_database() as database_name:
        _run_alembic(database_name, "upgrade", "20260730_0011")
        now = datetime.now(timezone.utc)
        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            db.execute(
                "INSERT INTO position_detail ("
                "position_detail_id, position_id, account_id, "
                "open_trade_id, order_book_id, exchange_id, symbol, "
                "direction, open_trading_day, open_price, pnl_base_price, "
                "original_volume, remaining_volume, frozen_volume, "
                "open_margin, remaining_margin, initial_occupied_margin, "
                "realtime_required_margin, multiplier_snapshot, "
                "open_commission, status, instrument_type, created_at, "
                "updated_at"
                ") VALUES ("
                "'PD-ORPHAN', 'P-ORPHAN', 'A-ORPHAN', 'T-MISSING', "
                "'MISSING', 'TEST', 'MISSING', 'LONG', CURRENT_DATE, "
                "100, 100, 1, 1, 0, 100, 100, 100, 100, 1, 0, "
                "'OPEN', 'FUTURES', %s, %s)",
                (now, now),
            )
            db.commit()

        with pytest.raises(subprocess.CalledProcessError):
            _run_alembic(database_name, "upgrade", "head")


def test_0014_repairs_option_account_aggregates_without_new_market_tick():
    """历史期权账户升级后立即具备正确聚合资金，不依赖未来行情修复。"""

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
            "%s, %s, %s, 'FUTURES', 100000, 100000, %s, 0, "
            "100000, %s, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "
            "'NORMAL', CURRENT_DATE, %s, %s"
        )
        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            db.execute(
                f"INSERT INTO account ({account_columns}) VALUES ({values})",
                (
                    "A-OPTION-REPAIR",
                    "LEGACY_OPTION_OWNER",
                    "历史期权账户",
                    Decimal("95000"),
                    Decimal("5000"),
                    now,
                    now,
                ),
            )
            db.execute(
                f"INSERT INTO account ({account_columns}) VALUES ({values})",
                (
                    "A-FUTURES-ONLY",
                    "LEGACY_FUTURES_OWNER",
                    "纯期货账户",
                    Decimal("50000"),
                    Decimal("0"),
                    now,
                    now,
                ),
            )
            db.commit()

        _run_alembic(database_name, "upgrade", "20260803_0013")
        position_sql = (
            "INSERT INTO position ("
            "position_id, account_id, order_book_id, exchange_id, symbol, "
            "direction, total_volume, today_volume, yesterday_volume, "
            "frozen_volume, available_volume, average_open_price, "
            "position_cost, used_margin, initial_occupied_margin, "
            "realtime_required_margin, option_market_value, "
            "multiplier_snapshot, realized_pnl, unrealized_pnl, "
            "daily_position_pnl, daily_close_pnl, trading_day, "
            "instrument_type, created_at, updated_at"
            ") VALUES ("
            "%s, 'A-OPTION-REPAIR', %s, 'DCE', %s, %s, %s, %s, 0, "
            "0, %s, 100, %s, %s, %s, %s, %s, 10, 0, 0, 0, 0, "
            "CURRENT_DATE, 'FUTURES_OPTION', %s, %s)"
        )
        detail_sql = (
            "INSERT INTO position_detail ("
            "position_detail_id, position_id, account_id, open_trade_id, "
            "order_book_id, exchange_id, symbol, direction, "
            "open_trading_day, open_price, pnl_base_price, "
            "original_volume, remaining_volume, frozen_volume, "
            "open_margin, remaining_margin, initial_occupied_margin, "
            "realtime_required_margin, multiplier_snapshot, "
            "open_commission, status, instrument_type, created_at, updated_at"
            ") VALUES ("
            "%s, %s, 'A-OPTION-REPAIR', %s, %s, 'DCE', %s, %s, "
            "CURRENT_DATE, 100, 100, %s, %s, 0, %s, %s, %s, %s, "
            "10, 0, 'OPEN', 'FUTURES_OPTION', %s, %s)"
        )
        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            rows = (
                (
                    "P-LONG",
                    "OPT-LONG",
                    "LONG",
                    2,
                    Decimal("2000"),
                    Decimal("0"),
                    Decimal("0"),
                ),
                (
                    "P-SHORT",
                    "OPT-SHORT",
                    "SHORT",
                    3,
                    Decimal("3000"),
                    Decimal("5000"),
                    Decimal("6000"),
                ),
            )
            for position_id, code, direction, volume, value, used, realtime in rows:
                db.execute(
                    position_sql,
                    (
                        position_id,
                        code,
                        code,
                        direction,
                        volume,
                        volume,
                        volume,
                        value,
                        used,
                        used,
                        realtime,
                        value,
                        now,
                        now,
                    ),
                )
                db.execute(
                    detail_sql,
                    (
                        f"D-{position_id}",
                        position_id,
                        f"T-{position_id}",
                        code,
                        code,
                        direction,
                        volume,
                        volume,
                        used,
                        used,
                        used,
                        realtime,
                        now,
                        now,
                    ),
                )
            # 制造历史聚合错误，0014必须仅根据PG持仓事实修复。
            db.execute(
                "UPDATE account SET long_option_market_value = 999, "
                "short_option_market_value = 888, "
                "net_option_market_value = 111, "
                "option_used_margin = 5000, "
                "option_realtime_required_margin = 777, "
                "equity = 1, available_cash = 2, risk_available_cash = 3 "
                "WHERE account_id = 'A-OPTION-REPAIR'"
            )
            db.commit()

        _run_alembic(database_name, "upgrade", "head")
        with psycopg.connect(_admin_dsn(database=database_name)) as db:
            repaired = db.execute(
                "SELECT long_option_market_value, short_option_market_value, "
                "net_option_market_value, option_realtime_required_margin, "
                "equity, available_cash, risk_available_cash, risk_state "
                "FROM account WHERE account_id = 'A-OPTION-REPAIR'"
            ).fetchone()
            futures_account = db.execute(
                "SELECT available_cash, equity FROM account "
                "WHERE account_id = 'A-FUTURES-ONLY'"
            ).fetchone()

        assert repaired == (
            Decimal("2000.000000"),
            Decimal("3000.000000"),
            Decimal("-1000.000000"),
            Decimal("6000.000000"),
            Decimal("99000.000000"),
            Decimal("92000.000000"),
            Decimal("91000.000000"),
            "NORMAL",
        )
        assert futures_account == (
            Decimal("50000.000000"),
            Decimal("100000.000000"),
        )
