from __future__ import annotations

from time import perf_counter
from uuid import uuid4

import structlog
from fastapi import Request, Response
from starlette.middleware.base import RequestResponseEndpoint


logger = structlog.get_logger(__name__)


async def log_request(request: Request, call_next: RequestResponseEndpoint) -> Response:
    request_id = request.headers.get("x-request-id") or str(uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    started_at = perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request_failed",
            method=request.method,
            path=request.url.path,
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
        )
        raise
    else:
        response.headers["x-request-id"] = request_id
        log_method = logger.warning if response.status_code >= 400 else logger.info
        log_method(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
        )
        return response
    finally:
        structlog.contextvars.clear_contextvars()
