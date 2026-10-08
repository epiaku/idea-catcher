"""The FastAPI app: one engine for its lifetime, the API keys, the error handlers and the routes."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from catcher import __version__
from catcher.api import routes_health, routes_items, routes_jobs, routes_runs, routes_youtube
from catcher.api.auth import bearer, parse_api_keys
from catcher.core.config import Settings
from catcher.core.db import WORKER_LOCK_TIMEOUT_MS

log = logging.getLogger("catcher.api")

CONNECT_TIMEOUT_S = 5  # a dead database answers /health fast


def make_api_engine(url: str) -> Engine:
    """Like the worker's engine (every session waits at most 10 s for a row lock), plus a short connect
    timeout so a request never hangs on a database that is gone."""
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "options": f"-c lock_timeout={WORKER_LOCK_TIMEOUT_MS}",
            "connect_timeout": CONNECT_TIMEOUT_S,
        },
    )


class CatchAll:
    """The last line: an unexpected exception becomes `500 {"detail": "internal error"}` and only its class
    is logged. Starlette's own handler for `Exception` would re-raise it after answering, and the server
    would then log the whole traceback (its message may hold data a log must not have)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except ClientDisconnect:  # the client went away mid-request: nobody to answer, not our error
            log.info("client disconnected in %s %s", scope.get("method"), scope.get("path"))
        except Exception as error:  # BaseException (CancelledError, KeyboardInterrupt) passes through
            log.error(
                "internal error in %s %s: %s", scope.get("method"), scope.get("path"), type(error).__name__
            )
            if started:  # the answer is already on its way: nothing better to send
                return
            await JSONResponse({"detail": "internal error"}, status_code=500)(scope, receive, send)


MAX_BODY_BYTES = 64 * 1024  # the trigger bodies are a few fields


class BodyLimit:
    """`413` for a request that announces a body over MAX_BODY_BYTES, before any route (or the auth) reads or
    parses it. A chunked body without a Content-Length is not measured here (the routes take tiny bodies)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            lengths = [v for k, v in scope.get("headers", []) if k == b"content-length"]
            if any(v.isdigit() and int(v) > MAX_BODY_BYTES for v in lengths):
                await JSONResponse({"detail": "request body too large"}, status_code=413)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def _database_error(request: Request, error: Exception) -> JSONResponse:
    """A database that is down or dropped the connection is `503 database unavailable`; any other database
    error is an internal error. Only the exception class is logged (its text may hold SQL or a host)."""
    where = f"{request.method} {request.url.path}"
    lost = isinstance(error, OperationalError | InterfaceError) or (
        isinstance(error, DBAPIError) and error.connection_invalidated
    )
    if lost:
        log.warning("database unavailable in %s: %s", where, type(error).__name__)
        return JSONResponse({"detail": "database unavailable"}, status_code=503)
    log.error("internal error in %s: %s", where, type(error).__name__)
    return JSONResponse({"detail": "internal error"}, status_code=500)


def create_app(settings: Settings | None = None) -> FastAPI:
    """The API app. Raises ValueError (naming the problem, never a key) when `API_KEYS` is not valid."""
    settings = settings or Settings()
    keys = parse_api_keys(settings.api_keys.get_secret_value())
    database_url = settings.database_url

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_api_engine(database_url)
        app.state.engine = engine
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="Idea Catcher",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.api_docs else None,
        openapi_url="/openapi.json" if settings.api_docs else None,
        redoc_url=None,
    )
    app.state.api_keys = keys
    app.state.youtube_block_hours = settings.youtube_block_hours
    app.add_exception_handler(DBAPIError, _database_error)
    app.add_middleware(BodyLimit)
    app.add_middleware(CatchAll)
    app.include_router(routes_health.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_items.router)
    app.include_router(routes_youtube.router)
    app.include_router(routes_runs.router)

    def openapi() -> dict[str, Any]:
        """The schema, with the Bearer scheme declared even before a route uses it (for Authorize)."""
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
            schemes.setdefault(
                bearer.scheme_name, bearer.model.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
    return app
