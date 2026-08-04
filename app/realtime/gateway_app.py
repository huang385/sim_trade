from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.core.logging_config import setup_logging
from app.realtime.gateway_runtime import GatewayRuntime
from app.realtime.metrics import realtime_metrics
from app.realtime.websocket_api import router


setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime_security()
    runtime = GatewayRuntime()
    app.state.runtime = runtime
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(
    title=f"{settings.app_name} WebSocket Gateway",
    version=settings.app_version,
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/health")
def health():
    runtime = app.state.runtime
    return {
        "status": "ok" if runtime.active else "error",
        "active_connections": runtime.manager.active_count,
        "single_instance_lease": runtime.active,
    }


@app.get("/metrics")
def metrics():
    """返回进程内WebSocket计数指标，便于本地压测和运行观察。"""

    return realtime_metrics.snapshot()
