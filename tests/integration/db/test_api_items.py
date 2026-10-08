from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from catcher.api.app import create_app
from catcher.core.config import Settings
from catcher.core.db import session_scope
from catcher.modules.queue.items import stage_item

pytestmark = pytest.mark.db

READER = "reader-key-0123456789abcdefghij"
BASE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
FIELDS = {
    "name",
    "doc_id",
    "doc_class",
    "status",
    "stage_reason",
    "stage_since",
    "profile",
    "backend",
    "model",
    "tokens_in",
    "tokens_out",
    "origin",
    "updated_at",
    "output_path",
}


@pytest.fixture
def client(pg_engine: Engine) -> Iterator[TestClient]:
    settings = Settings(
        database_url=pg_engine.url.render_as_string(hide_password=False),
        api_keys=SecretStr(f"reader:read:{READER}"),
    )
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {READER}"}) as test_client:
        yield test_client


def _item(db, name, doc_class="note", status="ready", minutes=0, **extra):
    item = stage_item(
        db,
        calculated_name=name,
        doc_id=name.split("/")[-1],
        doc_class=doc_class,
        now=BASE + timedelta(minutes=minutes),
        inbox_path="/secret/inbox/" + name,
        original_filename="orig-" + name,
    )
    item.status = status
    for key, value in extra.items():
        setattr(item, key, value)
    return item


def test_items_filter_by_class_status_and_stuck_and_page(client, pg_engine):
    with session_scope(pg_engine) as db:
        _item(db, "notes/a.md", minutes=1)
        _item(db, "notes/b.md", minutes=2, status="stuck", stage_reason="too long")
        _item(db, "clippings/c.md", doc_class="clipping", minutes=3, status="stuck")
        _item(db, "clippings/d.md", doc_class="clipping", minutes=4, status="published")
        _item(db, "notes/e.md", minutes=4)  # same updated_at as d: name breaks the tie

    body = client.get("/api/v1/items").json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert (body["total"], body["limit"], body["offset"]) == (5, 50, 0)
    assert [r["name"] for r in body["items"]] == [
        "clippings/d.md",
        "notes/e.md",
        "clippings/c.md",
        "notes/b.md",
        "notes/a.md",
    ]

    stuck = client.get("/api/v1/items", params={"status": "stuck"}).json()
    assert stuck["total"] == 2
    assert [r["name"] for r in stuck["items"]] == ["clippings/c.md", "notes/b.md"]
    assert stuck["items"][1]["stage_reason"] == "too long"

    clip = client.get("/api/v1/items", params={"doc_class": "clipping", "status": "stuck"}).json()
    assert [r["name"] for r in clip["items"]] == ["clippings/c.md"]

    page = client.get("/api/v1/items", params={"limit": 2, "offset": 3}).json()
    assert (page["total"], len(page["items"])) == (5, 2)
    assert [r["name"] for r in page["items"]] == ["notes/b.md", "notes/a.md"]

    for bad in ({"status": "bogus"}, {"doc_class": "x" * 33}, {"limit": 0}, {"limit": 201}, {"offset": -1}):
        assert client.get("/api/v1/items", params=bad).status_code == 422, bad
    assert client.get("/api/v1/items", params={"doc_class": "x'; --"}).json()["total"] == 0


def test_items_never_show_file_contents_or_secrets(client, pg_engine):
    with session_scope(pg_engine) as db:
        _item(
            db,
            "notes/a.md",
            output_path="notes/a.md",
            llm_profile="notes",
            llm_backend="freellm",
            llm_model="m",
            tokens_in=3,
            tokens_out=4,
            error="boom SECRET-ERROR",
            llm_result={"body": "FILE CONTENT SECRET"},
            warnings=["w"],
            docs_page="https://example.test/page",
            failed_path="/secret/failed/a.md",
        )

    row = client.get("/api/v1/items").json()["items"][0]

    assert set(row) == FIELDS
    assert row["profile"] == "notes" and row["backend"] == "freellm" and row["tokens_out"] == 4
    assert row["output_path"] == "notes/a.md"
    raw = client.get("/api/v1/items").text
    for secret in ("/secret/", "FILE CONTENT", "SECRET-ERROR", "orig-notes", "example.test"):
        assert secret not in raw


def test_the_answers_equal_direct_sql_counts(client, pg_engine):
    with session_scope(pg_engine) as db:
        for number in range(7):
            _item(db, f"notes/{number}.md", status="stuck" if number % 3 == 0 else "ready", minutes=number)
    with session_scope(pg_engine) as db:
        expected = db.execute(text("select count(*) from job_items where status = 'stuck'")).scalar_one()

    body = client.get("/api/v1/items", params={"status": "stuck"}).json()

    assert body["total"] == expected == len(body["items"]) == 3
