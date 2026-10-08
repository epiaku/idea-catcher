from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from catcher.api.app import create_app
from catcher.core.config import Settings
from catcher.core.db import session_scope

pytestmark = pytest.mark.db

READER = "reader-key-0123456789abcdefghij"
RUNNER = "runner-key-ABCDEFGHIJ9876543210"
ENDPOINTS = (
    "/api/v1/jobs",
    f"/api/v1/jobs/{'0' * 8}-0000-0000-0000-{'0' * 12}",
    "/api/v1/items",
    "/api/v1/youtube/gate",
)


@pytest.fixture
def client(pg_engine: Engine) -> Iterator[TestClient]:
    settings = Settings(
        database_url=pg_engine.url.render_as_string(hide_password=False),
        api_keys=SecretStr(f"reader:read:{READER} runner:run:{RUNNER}"),
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_gate_state_matches_catcher_youtube_gate_and_needs_read(client, pg_engine):
    assert client.get("/api/v1/youtube/gate").status_code == 401
    assert client.get("/api/v1/youtube/gate", headers=_bearer(RUNNER)).status_code == 403

    opened = client.get("/api/v1/youtube/gate", headers=_bearer(READER))
    assert opened.status_code == 200
    assert opened.json() == {
        "state": "open",
        "until": None,
        "streak": 0,
        "next_allowed_at": None,
        "blocked_until": None,
    }

    soon = datetime.now(UTC) + timedelta(hours=3)
    with session_scope(pg_engine) as db:
        db.execute(
            text(
                "update resources set blocked_until = :t, blocked_at = now(), streak = 2"
                " where name = 'youtube'"
            ),
            {"t": soon},
        )
    blocked = client.get("/api/v1/youtube/gate", headers=_bearer(READER)).json()
    assert blocked["state"] == "blocked" and blocked["streak"] == 2
    until = datetime.fromisoformat(blocked["until"])
    assert until.tzinfo is not None and abs((until - soon).total_seconds()) < 1
    assert blocked["blocked_until"] == blocked["until"]

    with session_scope(pg_engine) as db:
        db.execute(
            text(
                "update resources set blocked_until = null, streak = 0, next_allowed_at = :t"
                " where name = 'youtube'"
            ),
            {"t": datetime.now(UTC) + timedelta(minutes=2)},
        )
    gap = client.get("/api/v1/youtube/gate", headers=_bearer(READER)).json()
    assert gap["state"] == "gap" and gap["until"] == gap["next_allowed_at"]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_every_read_endpoint_needs_the_read_scope(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_bearer("x" * 30)).status_code == 401
    assert client.get(path, headers=_bearer(RUNNER)).status_code == 403
    assert client.get(path, headers=_bearer(READER)).status_code in (200, 404)
    assert client.get(path, headers=_bearer(READER)).status_code != 403
