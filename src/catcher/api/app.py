"""The FastAPI app: one engine for its lifetime, the API keys, the error handlers and the routes."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from catcher import __version__
from catcher.api import routes_health
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
        except Exception as error:
            log.error(
                "internal error in %s %s: %s", scope.get("method"), scope.get("path"), type(error).__name__
            )
            if started:  # the answer is already on its way: nothing better to send
                return
            await JSONResponse({"detail": "internal error"}, status_code=500)(scope, receive, send)


def _database_unavailable(request: Request, error: Exception) -> JSONResponse:
    log.warning("database unavailable in %s %s: %s", request.method, request.url.path, type(error).__name__)
    return JSONResponse({"detail": "database unavailable"}, status_code=503)


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
    app.add_exception_handler(OperationalError, _database_unavailable)
    app.add_middleware(CatchAll)
    app.include_router(routes_health.router)

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
