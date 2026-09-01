from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.core.config import settings


engine = create_engine(
    settings.database_url,
    pool_size=settings.postgres_pool_size,
    max_overflow=settings.postgres_max_overflow,
    pool_timeout=settings.postgres_pool_timeout_seconds,
    pool_recycle=settings.postgres_pool_recycle_seconds,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        # 业务服务通常会自行回滚；这里仍提供请求边界的最后保护，确保异常
        # 响应不会把失败事务或行锁带回连接池。
        db.rollback()
        raise
    finally:
        db.close()
