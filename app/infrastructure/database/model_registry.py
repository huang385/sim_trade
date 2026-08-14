"""SQLAlchemy 模型唯一注册入口，供应用、Alembic 和测试复用。"""

from app import models as _models  # noqa: F401
from app.core.database import Base

metadata = Base.metadata


def registered_table_names() -> frozenset[str]:
    return frozenset(metadata.tables)


__all__ = ["metadata", "registered_table_names"]
