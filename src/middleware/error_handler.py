"""Unified error handling.

Converts DomainException into an HTTP JSON response automatically.
Usage: call register_error_handlers(fastapi_app) in app.py.
"""

from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.domain.exceptions import DomainException, get_http_status_for_exception
from src.log import get_api_logger

logger = get_api_logger()


def register_error_handlers(app: FastAPI):
    """Register the domain exception handler on the FastAPI app."""

    @app.exception_handler(DomainException)
    async def domain_exception_handler(request: Request, exc: DomainException):
        status_code = get_http_status_for_exception(exc)

        response_body = {
            "error": exc.error_code,
            "message": exc.message,
            "timestamp": datetime.now().astimezone().isoformat(),
            "path": str(request.url.path),
        }
        if exc.details:
            response_body["details"] = exc.details

        if status_code >= 500:
            logger.error(f"[{exc.error_code}] {exc.message} | path={request.url.path}")
        else:
            logger.warning(f"[{exc.error_code}] {exc.message} | path={request.url.path}")

        return JSONResponse(status_code=status_code, content=response_body)
