"""`GET /api/v1/backfill` (`read` scope): the counts and a page of the backlog in the release order."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine

from catcher.api.app import create_app
from catcher.core.config import Settings
from catcher.core.db import session_scope
from catcher.modules.queue.models import BackfillVideo

pytestmark = pytest.mark.db

READER = "reader-key-0123456789abcdefghij"
RUNNER = "runner-key-0123456789abcdefghij"
URL = "/api/v1/backfill"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def client(pg_engine: Engine) -> Iterator[TestClient]:
    settings = Settings(
        database_url=pg_engine.url.render_as_string(hide_password=False),
        api_keys=SecretStr(f"reader:read:{READER} runner:run:{RUNNER}"),
    )
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {READER}"}) as test_client:
        yield test_client


def _seed(engine: Engine) -> None:
    rows = [
        ("vid_b", "docs", "a.md", "pending", 1, None),
        ("vid_a", "docs", "b.md", "pending", 1, None),
        ("vid_c", "channel", None, "pending", 2, None),
        ("vid_d", "docs", "c.md", "released", 0, 5),
        ("vid_e", "channel", None, "released", 0, 6),
    ]
    with session_scope(engine) as session:
        for video_id, source, found_in, status, found, released in rows:
            session.add(
                BackfillVideo(
                    video_id=video_id,
                    source=source,
                    found_in=found_in,
                    status=status,
                    found_at=BASE + timedelta(days=found),
                    released_at=None if released is None else BASE + timedelta(days=released),
                )
            )


def _ids(body: dict) -> list[str]:
    return [v["video_id"] for v in body["videos"]["items"]]


def test_counts_and_first_page(client, pg_engine):
    _seed(pg_engine)

    answer = client.get(URL)

    assert answer.status_code == 200
    body = answer.json()
    assert body["counts"] == {"pending": 3, "released": 2}
    assert body["videos"]["total"] == 5 and body["videos"]["limit"] == 50 and body["videos"]["offset"] == 0
    first = body["videos"]["items"][0]
    assert set(first) == {"video_id", "source", "found_in", "status", "found_at", "released_at"}
    assert (first["video_id"], first["source"], first["found_in"], first["status"]) == (
        "vid_d",
        "docs",
        "c.md",
        "released",
    )
    assert first["released_at"] is not None


def test_status_filter_pending_and_released(client, pg_engine):
    _seed(pg_engine)

    pending = client.get(URL, params={"status": "pending"}).json()
    released = client.get(URL, params={"status": "released"}).json()

    assert _ids(pending) == ["vid_a", "vid_b", "vid_c"] and pending["videos"]["total"] == 3
    assert _ids(released) == ["vid_d", "vid_e"] and released["videos"]["total"] == 2
    assert pending["counts"] == released["counts"] == {"pending": 3, "released": 2}


def test_source_filter(client, pg_engine):
    _seed(pg_engine)

    body = client.get(URL, params={"source": "channel"}).json()

    assert _ids(body) == ["vid_e", "vid_c"] and body["videos"]["total"] == 2


def test_order_is_the_release_order(client, pg_engine):
    _seed(pg_engine)

    body = client.get(URL, params={"status": "pending"}).json()
    assert _ids(body) == ["vid_a", "vid_b", "vid_c"]  # oldest found_at, then video_id

    page = client.get(URL, params={"status": "pending", "limit": 1, "offset": 1}).json()
    assert _ids(page) == ["vid_b"] and page["videos"]["total"] == 3


def test_bad_status_is_422(client):
    assert client.get(URL, params={"status": "nope"}).status_code == 422
    assert client.get(URL, params={"source": "nope"}).status_code == 422


def test_limit_201_is_422(client):
    assert client.get(URL, params={"limit": 201}).status_code == 422
    assert client.get(URL, params={"limit": 0}).status_code == 422
    assert client.get(URL, params={"offset": -1}).status_code == 422


def test_runner_only_key_gets_403(client):
    assert client.get(URL, headers={"Authorization": f"Bearer {RUNNER}"}).status_code == 403
    assert client.get(URL, headers={"Authorization": ""}).status_code == 401


def test_empty_backlog_returns_zero_counts_and_an_empty_page(client):
    body = client.get(URL).json()

    assert body["counts"] == {"pending": 0, "released": 0}
    assert body["videos"] == {"items": [], "total": 0, "limit": 50, "offset": 0}
