import asyncio
import logging
from collections.abc import Iterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from catcher.api.app import CatchAll, create_app
from catcher.api.auth import ApiKey, require
from catcher.api.deps import get_session
from catcher.core.config import Settings

pytestmark = pytest.mark.db

READER = "reader-key-0123456789abcdefghij"  # read only
RUNNER = "runner-key-ABCDEFGHIJ9876543210"  # run only
BOTH = "both-key-zyxwvutsrqponmlk0246813579"  # read and run
API_KEYS = f"reader:read:{READER} runner:run:{RUNNER} both:read,run:{BOTH}"
SECRETS = (READER, RUNNER, BOTH)


def _settings(pg_engine: Engine, **overrides) -> Settings:
    values = {
        "database_url": pg_engine.url.render_as_string(hide_password=False),
        "api_keys": SecretStr(API_KEYS),
        "api_docs": True,
    } | overrides
    return Settings(**values)


def _with_test_routes(app: FastAPI) -> FastAPI:
    """Routes that only the tests have: one per scope, one that touches the database, two that fail."""

    @app.get("/test/read")
    def read_route(key: Annotated[ApiKey, Depends(require("read"))]) -> dict[str, str]:
        return {"name": key.name}

    @app.post("/test/run")
    def run_route(key: Annotated[ApiKey, Depends(require("run"))]) -> dict[str, str]:
        return {"name": key.name}

    @app.get("/test/db")
    def db_route(
        _: Annotated[ApiKey, Depends(require("read"))], session: Annotated[Session, Depends(get_session)]
    ) -> dict[str, int]:
        return {"one": session.execute(text("select 1")).scalar_one()}

    @app.get("/test/db-error")
    def db_error_route(
        _: Annotated[ApiKey, Depends(require("read"))], session: Annotated[Session, Depends(get_session)]
    ) -> None:
        session.execute(text("select pg_terminate_backend(pg_backend_pid())"))

    @app.get("/test/crash")
    def crash_route(_: Annotated[ApiKey, Depends(require("read"))]) -> None:
        raise RuntimeError(f"secret detail {BOTH}")

    def _raiser(error: Exception):
        def route(_: Annotated[ApiKey, Depends(require("read"))]) -> None:
            raise error

        return route

    failures = {
        "interface": InterfaceError("select 1", {}, Exception(f"gone {BOTH}")),
        "invalidated": DBAPIError("select 1", {}, Exception(f"gone {BOTH}"), connection_invalidated=True),
        "integrity": IntegrityError("insert", {}, Exception(f"duplicate {BOTH}")),
    }
    for name, error in failures.items():
        app.add_api_route(f"/test/db-{name}", _raiser(error))

    return app


@pytest.fixture
def client(pg_engine: Engine) -> Iterator[TestClient]:
    with TestClient(_with_test_routes(create_app(_settings(pg_engine)))) as test_client:
        yield test_client


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_no_key_is_401_with_www_authenticate(client):
    for method, path in (("GET", "/test/read"), ("POST", "/test/run"), ("GET", "/test/db")):
        response = client.request(method, path)

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.json() == {"detail": "not authenticated"}


def test_a_wrong_key_and_a_prefix_of_a_valid_key_are_401(client):
    wrong = [
        "x" * 30,
        BOTH[:24],
        BOTH[:-1],
        BOTH + "x",
        BOTH.upper(),
        f"{READER}{RUNNER}",
    ]
    for key in wrong:
        response = client.get("/test/read", headers=_bearer(key))

        assert response.status_code == 401, key
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.json() == {"detail": "not authenticated"}

    # bytes outside ASCII (a header arrives as latin-1 text): no match, no crash
    response = client.get("/test/read", headers={"Authorization": ("Bearer " + "é" * 30).encode()})
    assert response.status_code == 401

    assert client.get("/test/read", headers=_bearer(BOTH)).json() == {"name": "both"}
    assert client.get("/test/read", headers=_bearer(READER)).json() == {"name": "reader"}
    assert client.get("/test/db", headers=_bearer(READER)).json() == {"one": 1}


def test_a_key_without_the_scope_is_403(client):
    # `run` does not imply `read`, and `read` does not imply `run`
    response = client.get("/test/read", headers=_bearer(RUNNER))
    assert response.status_code == 403
    assert response.json() == {"detail": "this key does not have the read scope"}
    assert "WWW-Authenticate" not in response.headers

    response = client.post("/test/run", headers=_bearer(READER))
    assert response.status_code == 403
    assert response.json() == {"detail": "this key does not have the run scope"}

    assert client.post("/test/run", headers=_bearer(RUNNER)).json() == {"name": "runner"}
    assert client.post("/test/run", headers=_bearer(BOTH)).json() == {"name": "both"}


def test_bearer_with_odd_spacing_or_an_empty_header_is_401_not_500(client):
    odd = [
        "",
        " ",
        "Bearer",
        "Bearer ",
        "Bearer    ",
        f"Bearer{BOTH}",
        f"Bearer\t{BOTH}",
        f"Bearer {BOTH} {BOTH}",
        f"Bearer {BOTH}x {READER}",
        f"Basic {BOTH}",
        f"Token {BOTH}",
        BOTH,
    ]
    for value in odd:
        response = client.get("/test/read", headers={"Authorization": value})

        assert response.status_code == 401, repr(value)
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.json() == {"detail": "not authenticated"}

    # two Authorization headers: not one valid key, so 401
    response = client.get("/test/read", headers=[("Authorization", f"Bearer {BOTH}"), ("Authorization", "x")])
    assert response.status_code == 401

    # RFC 6750 allows one or more spaces after the scheme, and the scheme is case-insensitive
    assert client.get("/test/read", headers={"Authorization": f"Bearer   {BOTH}"}).status_code == 200
    assert client.get("/test/read", headers={"Authorization": f"bearer {BOTH}"}).status_code == 200


def test_a_database_error_is_503_without_a_traceback_and_an_unexpected_error_is_500_without_details(
    client, caplog
):
    caplog.set_level(logging.DEBUG)

    response = client.get("/test/db-error", headers=_bearer(READER))
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    assert "Traceback" not in response.text and "terminat" not in response.text

    # the pool recovers: the next request gets a working connection
    assert client.get("/test/db", headers=_bearer(READER)).json() == {"one": 1}

    response = client.get("/test/crash", headers=_bearer(READER))
    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert "RuntimeError" in caplog.text  # the class is logged
    assert "secret detail" not in caplog.text and "Traceback" not in caplog.text
    assert BOTH not in caplog.text


def test_the_key_never_appears_in_openapi_logs_or_error_bodies(pg_engine, caplog):
    caplog.set_level(logging.DEBUG)
    app = _with_test_routes(create_app(_settings(pg_engine)))
    bodies = []
    with TestClient(app) as client:
        spec = client.get("/openapi.json")
        bodies.append(spec.text)
        bodies.append(client.get("/docs").text)
        for key in (READER, RUNNER, BOTH, BOTH[:24], "nope"):
            for method, path in (("GET", "/test/read"), ("POST", "/test/run"), ("GET", "/test/crash")):
                bodies.append(client.request(method, path, headers=_bearer(key)).text)
        bodies.append(client.get("/health").text)
        bodies.append(client.get("/test/nowhere", headers=_bearer(BOTH)).text)

    schemes = spec.json()["components"]["securitySchemes"]
    assert list(schemes.values()) == [{"type": "http", "scheme": "bearer"}]
    for secret in SECRETS:
        for part in (secret, secret[:8], secret[-8:]):
            assert part not in caplog.text
            for body in bodies:
                assert part not in body
    assert "API_KEYS" not in spec.text and "api_keys" not in spec.text


def test_docs_are_served_and_can_be_turned_off(pg_engine):
    with TestClient(create_app(_settings(pg_engine))) as client:
        assert client.get("/docs").status_code == 200
        assert "Swagger UI" in client.get("/docs").text
        spec = client.get("/openapi.json")
        assert spec.status_code == 200
        assert "/health" in spec.json()["paths"]
        assert "bearer" in str(spec.json()["components"]["securitySchemes"]).lower()

    with TestClient(create_app(_settings(pg_engine, api_docs=False))) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/health").status_code in (200, 503)  # the API itself still answers


def test_a_dropped_connection_is_503_and_another_database_error_is_500(client, caplog):
    caplog.set_level(logging.DEBUG)
    for name in ("interface", "invalidated"):
        response = client.get(f"/test/db-{name}", headers=_bearer(READER))
        assert response.status_code == 503, name
        assert response.json() == {"detail": "database unavailable"}

    response = client.get("/test/db-integrity", headers=_bearer(READER))
    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert "IntegrityError" in caplog.text
    assert BOTH not in caplog.text and "gone" not in caplog.text and "duplicate" not in caplog.text


def test_a_client_that_disconnects_is_not_logged_as_an_internal_error(caplog):
    caplog.set_level(logging.DEBUG)
    sent = []

    async def leaving_client_app(scope, receive, send):
        raise ClientDisconnect()

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/test/slow"}
    asyncio.run(CatchAll(leaving_client_app)(scope, receive, send))

    assert sent == []  # nobody to answer
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert "client disconnected in GET /test/slow" in caplog.text
