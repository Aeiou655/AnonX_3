"""Error handlers for FastAPI."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from AnonX_3.downloader_api.core.exceptions import DownloaderAPIError
from AnonX_3.downloader_api.schemas.error import ErrorResponse, ErrorDetail

logger = logging.getLogger(__name__)


def create_error_response(
    request_id: str,
    code: str,
    message: str,
    retryable: bool,
    job_id: str | None = None,
) -> dict[str, Any]:
    return ErrorResponse(
        success=False,
        error=ErrorDetail(
            code=code,
            message=message,
            retryable=retryable,
        ),
        request_id=request_id,
        job_id=job_id,
    ).model_dump()


async def downloader_api_error_handler(
    request: Request, exc: DownloaderAPIError
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    job_id = getattr(request.state, "job_id", None)

    logger.warning(
        "API error",
        extra={
            "request_id": request_id,
            "error_code": exc.code,
            "error_message": exc.message,
            "status_code": exc.status_code,
            "retryable": exc.retryable,
        },
    )

    return JSONResponse(
        status_code=exc.status_code,
        content=create_error_response(
            request_id=request_id,
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            job_id=job_id,
        ),
        headers={"X-Request-ID": request_id},
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")

    logger.exception(
        "Unhandled exception",
        extra={"request_id": request_id},
    )

    return JSONResponse(
        status_code=500,
        content=create_error_response(
            request_id=request_id,
            code="INTERNAL_ERROR",
            message="An internal error occurred",
            retryable=False,
        ),
        headers={"X-Request-ID": request_id},
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DownloaderAPIError, downloader_api_error_handler)
    app.add_exception_handler(Exception, generic_exception_handler)
