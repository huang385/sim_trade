from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

# 必须导入models，确保SQLAlchemy知道有哪些表
from app import models
from app.api.exception_handlers import app_error_handler
from app.api.router import api_router
from app.common.exceptions import AppError
from app.core.config import settings
from app.core.database import engine
from app.core.logging_config import setup_logging
from app.core.redis_client import check_redis


# 初始化日志
setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI启动和关闭生命周期。

    数据库结构由 Alembic 迁移管理。启动 API 前应先执行：
    alembic upgrade head
    """

    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)


# 注册统一业务异常处理器
app.add_exception_handler(
    AppError,
    app_error_handler,
)


# 注册全部业务接口
app.include_router(api_router)


@app.get("/")
def root():
    """
    根路径。
    """

    return {
        "message": "sim trade system running",
        "version": settings.app_version,
    }


@app.get("/health")
def health_check():
    """
    检查PostgreSQL和Redis连接状态。
    """

    postgres_ok: bool | str = False
    redis_ok: bool | str = False

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        postgres_ok = True

    except Exception as exc:
        postgres_ok = str(exc)

    try:
        redis_ok = check_redis()

    except Exception as exc:
        redis_ok = str(exc)

    return {
        "status": (
            "ok"
            if postgres_ok is True and redis_ok is True
            else "error"
        ),
        "postgres": postgres_ok,
        "redis": redis_ok,
    }
