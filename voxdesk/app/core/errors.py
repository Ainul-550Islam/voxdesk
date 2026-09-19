"""Centralised error handling, request correlation, and sanitised responses.

Philosophy (fail-closed, zero-trust friendly)
----------------------------------------------
* Every response carries an ``X-Request-ID`` header so a customer-reported
  failure can be correlated with a log line in seconds.
* Unhandled exceptions are logged in full server-side (stack trace included)
  but are never echoed to the client in production: the client sees a stable
  machine-readable code and a request id, nothing else.
* Validation errors keep FastAPI's documented ``{"detail": [...]}`` shape and
  framework 4xx responses (auth 401/403, etc.) are passed through untouched
  apart from the request id, so existing clients are unaffected.
* Application code raises the ``AppError`` family to produce a consistent
  ``{"error": {"code", "message", "request_id", ...}}`` body with the correct
  HTTP status.

Wire it up from ``app/main.py``::

    from app.core.errors import install_error_handling
    install_error_handling(app)

Nothing else is required; the module owns its middleware and its handlers.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.logging import log

__all__ = [
    "AppError",
    "BadRequestError",
    "ConflictError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitedError",
    "ServiceUnavailableError",
    "install_error_handling",
    "request_id_from",
    "new_request_id",
    "REQUEST_ID_HEADER",
]

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_LIMIT = 64
_BODYLESS_STATUSES = {204, 304}


# ------------------------------------------------------------------ errors ---

class AppError(Exception):
    """Base class for domain errors raised by application code.

    ``message`` is always safe to show to the caller. ``detail`` is optional
    extra context that is *also* returned to the caller, so it must be free of
    secrets -- leave it ``None`` for internal-only information.
    """

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str = "Internal server error",
        *,
        code: str | None = None,
        detail: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.headers = headers
        if code is not None:
            self.code = code


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"


class PermissionDeniedError(AppError):
    status_code = 403
    code = "permission_denied"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(
        self,
        message: str = "Too many requests",
        *,
        retry_after: int | None = None,
        code: str | None = None,
        detail: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        merged = dict(headers) if headers else {}
        if retry_after is not None:
            merged["Retry-After"] = str(retry_after)
        super().__init__(
            message, code=code, detail=detail, headers=merged or None
        )


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"


# ------------------------------------------------------------ request id ---

def new_request_id() -> str:
    """A fresh correlation id."""
    return uuid.uuid4().hex


def _sanitize_request_id(raw: str | None) -> str:
    """Accept a caller-supplied id only when it is short and safe.

    This prevents header injection and log forging through a hostile
    ``X-Request-ID`` value; anything else gets a freshly generated id.
    """
    if raw and 0 < len(raw) <= _REQUEST_ID_LIMIT:
        if all(c.isalnum() or c in "-_." for c in raw):
            return raw
    return new_request_id()


def request_id_from(request: Request) -> str:
    """The correlation id for ``request`` (from state, header, or generated)."""
    rid = getattr(request.state, "request_id", None)
    if not rid:
        rid = _sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = rid
    return rid


# -------------------------------------------------------------- responses ---

def _headers_with_request_id(
    extra: dict[str, str] | None, rid: str
) -> dict[str, str]:
    merged = {REQUEST_ID_HEADER: rid}
    if extra:
        merged.update(extra)
    return merged


def _error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    *,
    detail: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {"code": code, "message": message, "request_id": request_id}
    }
    if detail is not None:
        body["error"]["detail"] = detail
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers=_headers_with_request_id(headers, request_id),
    )


# ----------------------------------------------------------------- handlers ---

async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    rid = request_id_from(request)
    log.warning(
        "app_error",
        code=exc.code,
        status=exc.status_code,
        request_id=rid,
        message=exc.message,
    )
    return _error_response(
        exc.status_code,
        exc.code,
        exc.message,
        rid,
        detail=exc.detail,
        headers=exc.headers,
    )


async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    rid = request_id_from(request)
    status = exc.status_code

    if status >= 500:
        # A framework-level 500 must not leak its detail to the caller.
        log.exception(
            "http_exception_500", status=status, request_id=rid, detail=str(exc.detail)
        )
        return _error_response(status, "internal_error", "Internal server error", rid)

    if status in _BODYLESS_STATUSES or 100 <= status < 200:
        return Response(
            status_code=status, headers=_headers_with_request_id(exc.headers, rid)
        )

    log.warning(
        "http_exception", status=status, request_id=rid, detail=exc.detail
    )
    # Preserve FastAPI's default JSON shape for client compatibility.
    return JSONResponse(
        status_code=status,
        content={"detail": exc.detail},
        headers=_headers_with_request_id(exc.headers, rid),
    )


async def _validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    rid = request_id_from(request)
    log.warning("request_validation_error", request_id=rid, errors=exc.errors())
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors())},
        headers=_headers_with_request_id(None, rid),
    )


async def _unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    rid = request_id_from(request)
    # Full trace server-side; the client never sees the exception itself in
    # production. Development gets the repr to speed up debugging.
    log.exception("unhandled_exception", request_id=rid)
    detail = None if settings.is_production else repr(exc)
    return _error_response(500, "internal_error", "Internal server error", rid, detail=detail)


# ------------------------------------------------------------------- wiring ---

def add_request_id_middleware(app: FastAPI) -> None:
    """Attach the request-id middleware to ``app``."""

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):
        rid = _sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = rid
        structlog.contextvars.bind_contextvars(request_id=rid)
        try:
            response = await call_next(request)
            if REQUEST_ID_HEADER not in response.headers:
                response.headers[REQUEST_ID_HEADER] = rid
            return response
        finally:
            structlog.contextvars.unbind_contextvars("request_id")


def install_error_handling(app: FastAPI) -> None:
    """Register every handler and the request-id middleware on ``app``."""
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
    add_request_id_middleware(app)
