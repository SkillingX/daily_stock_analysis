# -*- coding: utf-8 -*-
"""
===================================
全局异常处理中间件
===================================

职责：
1. 捕获未处理的异常
2. 统一错误响应格式
3. 记录错误日志
"""

import logging
import traceback
from typing import Any, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from typing import cast  # added by mypy_codemod

logger = logging.getLogger(__name__)


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """
    全局异常处理中间件

    捕获所有未处理的异常，返回统一格式的错误响应
    """

    async def dispatch(
        self, request: Request, call_next: Callable[..., Any]
    ) -> Response:
        """
        处理请求，捕获异常

        Args:
            request: 请求对象
            call_next: 下一个处理器

        Returns:
            Response: 响应对象
        """
        try:
            response = await call_next(request)
            return cast(Response, response)

        except Exception as e:
            # 记录错误日志
            logger.error(
                f"未处理的异常: {e}\n"
                f"请求路径: {request.url.path}\n"
                f"请求方法: {request.method}\n"
                f"堆栈: {traceback.format_exc()}"
            )

            # 返回统一格式的错误响应
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "message": "服务器内部错误，请稍后重试",
                    "detail": str(e) if logger.isEnabledFor(logging.DEBUG) else None,
                },
            )


def add_error_handlers(app) -> None:
    """
    添加全局异常处理器

    为 FastAPI 应用添加各类异常的处理器

    Args:
        app: FastAPI 应用实例
    """
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError
    from src.core.market_policy import MarketDisabledError

    @app.exception_handler(MarketDisabledError)
    async def market_disabled_handler(request: Request, exc: MarketDisabledError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": "market_disabled", "message": str(exc), "detail": None})

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        """处理 HTTP 异常"""
        # 如果 detail 已经是 ErrorResponse 格式的 dict，直接使用
        if (
            isinstance(exc.detail, dict)
            and "error" in exc.detail
            and "message" in exc.detail
        ):
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        # 否则将 detail 包装成 ErrorResponse 格式
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": "http_error",
                "message": str(exc.detail) if exc.detail else "HTTP Error",
                "detail": None,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        """处理请求验证异常。``ctx.error`` 可能含非 JSON 可序列化对象（如 ValueError），
        需要预先转 str 避免 JSONResponse 序列化失败被外层 Exception handler 兜底成 500。"""
        safe_errors = []
        for err in exc.errors():
            err_copy = dict(err)
            ctx = err_copy.get("ctx")
            if isinstance(ctx, dict):
                err_copy["ctx"] = {
                    k: str(v) if isinstance(v, Exception) else v for k, v in ctx.items()
                }
            safe_errors.append(err_copy)
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "请求参数验证失败",
                "detail": safe_errors,
            },
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        """处理通用异常"""
        logger.error(
            f"未处理的异常: {exc}\n"
            f"请求路径: {request.url.path}\n"
            f"堆栈: {traceback.format_exc()}"
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "message": "服务器内部错误",
                "detail": None,
            },
        )
