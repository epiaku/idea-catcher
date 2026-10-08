import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine

from catcher.api.app import create_app
from catcher.core.config import Settings
from catcher.core.db import session_scope
from catcher.modules.queue.items import stage_item
from catcher.modules.queue.models import Job, JobEvent

pytestmark = pytest.mark.db

READER = "reader-key-0123456789abcdefghij"
BASE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(pg_engine: Engine) -> Iterator[TestClient]:
    settings = Settings(
        database_url=pg_engine.url.render_as_string(hide_password=False),
        api_keys=SecretStr(f"reader:read:{READER}"),
    )
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {READER}"}) as test_client:
        yield test_client


def _job(session, *, type="pipeline.run", status="succeeded", minutes=0, **extra) -> Job:
    at = BASE + timedelta(minutes=minutes)
    job = Job(
        id=uuid.uuid4(),
        type=type,
        status=status,
        run_after=at,
        params={"n": 1},
        created_at=at,
        lease_until=at if status == "running" else None,
        **extra,
    )
    session.add(job)
    return job


def test_jobs_list_is_newest_first_paged_and_filtered_by_type_status_and_dates(client, pg_engine):
    with session_scope(pg_engine) as db:
        for minute in range(5):
            _job(db, minutes=minute)
        _job(db, type="ideas.pull", status="failed", minutes=10, error="boom", reason="why")
        _job(db, type="ideas.pull", status="queued", minutes=11)

    body = client.get("/api/v1/jobs").json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert (body["total"], body["limit"], body["offset"]) == (7, 50, 0)
    created = [row["created_at"] for row in body["items"]]
    assert created == sorted(created, reverse=True)
    assert created[0].startswith("2026-10-01T12:11:00") and (
        "+00:00" in created[0] or created[0].endswith("Z")
    )
    first = body["items"][1]
    assert {"id", "type", "status", "priority", "params", "result", "error", "reason", "attempts"} <= set(
        first
    )
    assert {"run_after", "created_at", "started_at", "finished_at"} <= set(first)

    page = client.get("/api/v1/jobs", params={"limit": 2, "offset": 2}).json()
    assert (page["total"], page["limit"], page["offset"], len(page["items"])) == (7, 2, 2, 2)
    assert [r["id"] for r in page["items"]] == [r["id"] for r in body["items"][2:4]]

    pulls = client.get("/api/v1/jobs", params={"type": "ideas.pull"}).json()
    assert pulls["total"] == 2
    failed = client.get("/api/v1/jobs", params={"type": "ideas.pull", "status": "failed"}).json()
    assert failed["total"] == 1 and failed["items"][0]["error"] == "boom"

    window = {"from": "2026-10-01T12:01:00Z", "to": "2026-10-01T12:03:00+00:00"}
    assert client.get("/api/v1/jobs", params=window).json()["total"] == 3
    naive = {"from": "2026-10-01T12:01:00", "to": "2026-10-01T12:03:00"}  # naive means UTC
    assert client.get("/api/v1/jobs", params=naive).json()["total"] == 3
    offset_form = {"from": "2026-10-01T14:01:00+02:00", "to": "2026-10-01T14:03:00+02:00"}
    assert client.get("/api/v1/jobs", params=offset_form).json()["total"] == 3


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"limit": -1},
        {"limit": "abc"},
        {"offset": -1},
        {"offset": "x"},
        {"status": "bogus"},
        {"status": "queued' OR '1'='1"},
        {"status": "QUEUED"},
        {"type": "x" * 65},
        {"from": "yesterday"},
        {"to": "2026-13-45T99:00:00"},
        {"from": "'; DROP TABLE jobs;--"},
        {"limit": 10**30},
    ],
)
def test_jobs_list_rejects_bad_query_values_with_422(client, params):
    response = client.get("/api/v1/jobs", params=params)

    assert response.status_code == 422


def test_hostile_text_in_type_is_only_a_value(client, pg_engine):
    with session_scope(pg_engine) as db:
        _job(db)
    hostile = "x'; DROP TABLE jobs;--%_"

    assert client.get("/api/v1/jobs", params={"type": hostile}).json()["total"] == 0
    assert client.get("/api/v1/jobs", params={"type": "%"}).json()["total"] == 0
    assert client.get("/api/v1/jobs").json()["total"] == 1


def test_job_detail_has_events_and_item_counts_for_a_run(client, pg_engine):
    with session_scope(pg_engine) as db:
        run = _job(db)
        other = _job(db, type="ideas.pull", minutes=1)
        db.flush()
        for number in range(205):
            db.add(
                JobEvent(
                    job_id=run.id, ts=BASE + timedelta(seconds=number), level="info", message=f"e{number}"
                )
            )
        db.add(JobEvent(job_id=other.id, ts=BASE, level="info", message="other"))
        for name, doc_class, status in (
            ("clippings/a.md", "clipping", "published"),
            ("clippings/b.md", "clipping", "published"),
            ("clippings/c.md", "clipping", "failed"),
            ("notes/a.md", "note", "ready"),
        ):
            stage_item(
                db,
                calculated_name=name,
                doc_id=name,
                doc_class=doc_class,
                now=BASE,
                inbox_path=None,
                original_filename=None,
                root_job_id=run.id,
            ).status = status
        stage_item(
            db, calculated_name="notes/z.md", doc_id="z", doc_class="note", now=BASE, inbox_path=None,
            original_filename=None, root_job_id=other.id,
        )  # fmt: skip
        run_id, other_id = run.id, other.id

    detail = client.get(f"/api/v1/jobs/{run_id}").json()
    assert detail["id"] == str(run_id)
    assert len(detail["events"]) == 200
    assert detail["events"][0]["message"] == "e0" and detail["events"][1]["message"] == "e1"
    assert {"ts", "level", "message", "data", "item_id"} <= set(detail["events"][0])
    assert detail["item_counts"] == {"clipping": {"published": 2, "failed": 1}, "note": {"ready": 1}}

    plain = client.get(f"/api/v1/jobs/{other_id}").json()
    assert plain["item_counts"] == {}
    assert [event["message"] for event in plain["events"]] == ["other"]


def test_job_detail_404_for_an_unknown_or_malformed_id(client):
    unknown = client.get(f"/api/v1/jobs/{uuid.uuid4()}")
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "job not found"}

    for bad in ("not-a-uuid", "1", "%27%20OR%201=1", "0" * 40):
        assert client.get(f"/api/v1/jobs/{bad}").status_code == 404, bad


def test_the_answers_equal_direct_sql_counts(client, pg_engine):
    from sqlalchemy import text

    with session_scope(pg_engine) as db:
        for minute, status in enumerate(("queued", "failed", "failed", "succeeded", "cancelled")):
            _job(db, status=status, type="ideas.pull" if minute % 2 else "pipeline.run", minutes=minute)
    with session_scope(pg_engine) as db:
        expected = db.execute(
            text("select count(*) from jobs where status = 'failed' and type = 'ideas.pull'")
        ).scalar_one()

    body = client.get("/api/v1/jobs", params={"status": "failed", "type": "ideas.pull"}).json()

    assert body["total"] == expected == len(body["items"])
