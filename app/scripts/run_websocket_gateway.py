import uvicorn

from app.core.config import settings


def main() -> None:
    """独立启动第一版单Worker WebSocket Gateway。"""

    uvicorn.run(
        "app.realtime.gateway_app:app",
        host=settings.ws_gateway_host,
        port=settings.ws_gateway_port,
        workers=1,
        reload=False,
        # Ticket位于查询参数中；关闭Uvicorn访问日志，避免正常日志泄露票据。
        # 业务安全日志只记录connection_id和必要的user_id。
        access_log=False,
    )


if __name__ == "__main__":
    main()
