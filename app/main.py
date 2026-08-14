from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

# 必须导入models，确保SQLAlchemy知道有哪些表
from app import models  # noqa: F401  # 注册全部SQLAlchemy表
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

    # 开发环境允许只启动公开健康检查；生产环境则必须在监听请求前完成
    # JWT Secret和Refresh Cookie安全校验，禁止带弱配置继续运行。
    settings.validate_runtime_security()
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)

# Tauri WebView 与交易 API 拥有不同 Origin。白名单只包含配置的桌面来源，
# Refresh Cookie 因此可以携带，同时不会把交易 API 开放给任意网页。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# 测试交易台与 API 同源部署，页面请求无需额外配置 CORS。
# 静态目录只存放调试页面资源，不参与任何交易业务计算。
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static",
)


# 注册统一业务异常处理器
app.add_exception_handler(
    AppError,
    app_error_handler,
)


# 注册全部业务接口
app.include_router(api_router)


@app.get("/test-trading", include_in_schema=False)
def trading_test_page():
    """
    返回本地交易与实时盈亏测试台。

    该页面用于开发环境手工联调，所有数据仍通过正式 API 读取或提交，
    不会绕过订单服务、资金校验、撮合和成交结算流程。
    """

    return FileResponse(STATIC_DIR / "trading_test.html")


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
