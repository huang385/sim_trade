import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from app.common.exceptions import AppError


logger = logging.getLogger(__name__)


async def app_error_handler(
    request: Request,
    exc: AppError,
) -> JSONResponse:
    """
    将业务异常转换成统一HTTP响应。
    """

    logger.warning(
        "业务异常 path=%s error_code=%s message=%s",
        request.url.path,
        exc.error_code,
        exc.message,
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error_code": exc.error_code,
            "message": exc.message,
        },
    )