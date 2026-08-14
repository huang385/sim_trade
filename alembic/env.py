from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 加载全部模型，确保 autogenerate 能看到完整 metadata。
from app.core.config import settings
from app.infrastructure.database.model_registry import metadata


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 数据库地址统一取项目 Settings，不在 alembic.ini 中重复保存密码。
config.set_main_option("sqlalchemy.url", settings.database_url)
target_metadata = metadata

# 交易日历和产品时段由独立参考数据同步链路管理，本项目只读复用。自动
# 比对必须忽略它们，否则 Alembic 会错误建议删除这两张已有事实表。
EXTERNALLY_MANAGED_TABLES = {
    "trading_calendar",
    "product_trading_schedule",
}


def include_name(name, type_, parent_names):
    if (
        type_ == "table"
        and name in EXTERNALLY_MANAGED_TABLES
        and parent_names.get("schema_name") in {None, "public"}
    ):
        return False
    return True


def run_migrations_offline() -> None:
    """在不创建数据库连接的情况下输出迁移 SQL。"""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """连接 PostgreSQL 并执行迁移。"""

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
