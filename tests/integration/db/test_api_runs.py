"""The trigger endpoints (`run` scope): start a run or a preview, publish, requeue one item. They only enqueue
a job and answer; the worker does the work. A run or publish already queued or running (scheduled, from the
CLI or from the API) is answered `200` with that job; two requests at the same moment make one job."""

import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select

from catcher.api import routes_runs
from catcher.api.app import create_app
from catcher.core.config import Settings
from catcher.core.db import session_scope
from catcher.modules.queue.items import stage_item
from catcher.modules.queue.models import Job
from catcher.modules.queue.queue import enqueue

pytestmark = pytest.mark.db

READER = "reader-key-0123456789abcdefghij"
RUNNER = "runner-key-0123456789abcdefghij"
BOTH = "both-key-0123456789abcdefghijkl"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(pg_engine: Engine) -> Iterator[TestClient]:
    settings = Settings(
        database_url=pg_engine.url.render_as_string(hide_password=False),
        api_keys=SecretStr(f"reader:read:{READER} runner:run:{RUNNER} both:read,run:{BOTH}"),
    )
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {RUNNER}"}) as test_client:
        yield test_client


def _jobs(engine: Engine) -> list[Job]:
    with session_scope(engine) as session:
        return list(session.scalars(select(Job).order_by(Job.created_at, Job.id)))


def _set_status(engine: Engine, job_id: str, status: str) -> None:
    with session_scope(engine) as session:
        job = session.get(Job, uuid.UUID(job_id))
        assert job is not None
        job.status = status
        job.lease_until = NOW if status == "running" else None


def test_post_runs_enqueues_pipeline_run_with_202(client, pg_engine):
    answer = client.post("/api/v1/pipeline/runs")

    assert answer.status_code == 202
    body = answer.json()
    assert set(body) == {"job_id", "existing"} and body["existing"] is False  # no params, no key
    [job] = _jobs(pg_engine)
    assert (str(job.id), job.type, job.status, job.params) == (body["job_id"], "pipeline.run", "queued", {})
    assert job.dedupe_key == "api:pipeline.run"

    _set_status(pg_engine, body["job_id"], "succeeded")
    given = client.post(
        "/api/v1/pipeline/runs",
        json={"dry_run": False, "profile": "notes", "limit": 3, "retry_deferred": True},
    )
    assert given.status_code == 202
    job = _jobs(pg_engine)[-1]
    assert job.params == {"profile": "notes", "limit": 3, "retry_deferred": True}
    assert str(job.id) == given.json()["job_id"]


def test_a_second_post_while_one_is_queued_or_running_is_200_with_the_same_job_id(client, pg_engine):
    first = client.post("/api/v1/pipeline/runs").json()

    again = client.post("/api/v1/pipeline/runs", json={"limit": 1})  # other params: still the same job
    assert again.status_code == 200
    assert again.json() == {"job_id": first["job_id"], "existing": True}

    _set_status(pg_engine, first["job_id"], "running")
    running = client.post("/api/v1/pipeline/runs")
    assert running.status_code == 200 and running.json() == {"job_id": first["job_id"], "existing": True}

    _set_status(pg_engine, first["job_id"], "failed")
    after = client.post("/api/v1/pipeline/runs")
    assert after.status_code == 202 and after.json()["job_id"] != first["job_id"]
    assert len(_jobs(pg_engine)) == 2


def test_a_scheduled_pipeline_run_counts_as_existing(client, pg_engine):
    with session_scope(pg_engine) as session:
        scheduled, _ = enqueue(
            session, type="pipeline.run", now=NOW, params={}, dedupe_key="schedule:pipeline_run"
        )
        scheduled_id = str(scheduled.id)
    answer = client.post("/api/v1/pipeline/runs")
    assert answer.status_code == 200 and answer.json() == {"job_id": scheduled_id, "existing": True}

    # one without a dedupe key (`catcher jobs add pipeline.run`) counts too; the oldest is answered
    _set_status(pg_engine, scheduled_id, "succeeded")
    with session_scope(pg_engine) as session:
        older, _ = enqueue(session, type="pipeline.run", now=NOW, params={"limit": 2})
        enqueue(session, type="pipeline.run", now=NOW.replace(minute=5), params={})
        older_id = str(older.id)
    answer = client.post("/api/v1/pipeline/runs")
    assert answer.status_code == 200 and answer.json() == {"job_id": older_id, "existing": True}
    assert len(_jobs(pg_engine)) == 3


def test_a_requeue_or_only_job_is_not_the_existing_run(client, pg_engine):
    with session_scope(pg_engine) as session:
        stage_item(
            session,
            calculated_name="notes/walks.md",
            doc_id="walks",
            doc_class="note",
            now=NOW,
            inbox_path=None,
            original_filename=None,
        )
        only, _ = enqueue(session, type="pipeline.run", now=NOW, params={"only": ["walks"]})  # CLI style
        only_id = str(only.id)
    requeued = client.post("/api/v1/items/notes/walks.md/requeue").json()

    answer = client.post("/api/v1/pipeline/runs")

    assert answer.status_code == 202 and answer.json()["existing"] is False
    assert answer.json()["job_id"] not in (only_id, requeued["job_id"])
    full = _jobs(pg_engine)[-1]
    assert (full.params, full.dedupe_key) == ({}, "api:pipeline.run")
    again = client.post("/api/v1/pipeline/runs")  # the full run is the existing one now
    assert again.status_code == 200 and again.json() == {"job_id": answer.json()["job_id"], "existing": True}
    assert len(_jobs(pg_engine)) == 3


def test_a_scheduled_run_with_retry_deferred_still_counts_beside_a_requeue(client, pg_engine):
    with session_scope(pg_engine) as session:
        enqueue(session, type="pipeline.run", now=NOW, params={"requeue": ["notes/x.md"]})
        scheduled, _ = enqueue(
            session,
            type="pipeline.run",
            now=NOW.replace(minute=5),
            params={"retry_deferred": True},
            dedupe_key="schedule:pipeline_run",
        )
        scheduled_id = str(scheduled.id)

    answer = client.post("/api/v1/pipeline/runs")

    assert answer.status_code == 200 and answer.json() == {"job_id": scheduled_id, "existing": True}


def test_two_simultaneous_posts_after_a_requeue_make_one_run(client, pg_engine, monkeypatch):
    with session_scope(pg_engine) as session:
        enqueue(session, type="pipeline.run", now=NOW, params={"requeue": ["notes/x.md"]})
    barrier = threading.Barrier(2)
    looked_up = routes_runs.active_job

    def both_look_first(*args):
        found = looked_up(*args)
        barrier.wait(timeout=10)
        return found

    monkeypatch.setattr(routes_runs, "active_job", both_look_first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda _: client.post("/api/v1/pipeline/runs"), range(2)))

    assert sorted(a.status_code for a in answers) == [200, 202]
    assert answers[0].json()["job_id"] == answers[1].json()["job_id"]
    assert [j.params for j in _jobs(pg_engine)] == [{"requeue": ["notes/x.md"]}, {}]


@pytest.mark.parametrize("path", ["/api/v1/pipeline/runs", "/api/v1/pipeline/publish"])
def test_a_body_over_64_kb_is_413_before_it_is_parsed(client, pg_engine, path):
    big = b'{"profile": "' + b"p" * (64 * 1024) + b'"}'
    answer = client.post(path, content=big, headers={"Content-Type": "application/json"})
    assert answer.status_code == 413 and answer.json() == {"detail": "request body too large"}
    unauthenticated = client.post(path, content=big, headers={"Authorization": ""})
    assert unauthenticated.status_code == 413
    assert _jobs(pg_engine) == []


def test_two_simultaneous_posts_make_one_job(client, pg_engine, monkeypatch):
    barrier = threading.Barrier(2)
    looked_up = routes_runs.active_job

    def both_look_first(*args):
        found = looked_up(*args)
        barrier.wait(timeout=10)  # both requests found nothing before either inserts
        return found

    monkeypatch.setattr(routes_runs, "active_job", both_look_first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda _: client.post("/api/v1/pipeline/runs"), range(2)))

    assert sorted(a.status_code for a in answers) == [200, 202]
    assert answers[0].json()["job_id"] == answers[1].json()["job_id"]
    assert sorted(a.json()["existing"] for a in answers) == [False, True]
    assert len(_jobs(pg_engine)) == 1


def test_dry_run_enqueues_pipeline_preview_not_a_run(client, pg_engine):
    run = client.post("/api/v1/pipeline/runs").json()  # a queued run does not stop a preview

    answer = client.post("/api/v1/pipeline/runs", json={"dry_run": True, "limit": 2})

    assert answer.status_code == 202 and answer.json()["existing"] is False
    assert answer.json()["job_id"] != run["job_id"]
    preview = _jobs(pg_engine)[-1]
    assert (preview.type, preview.status, preview.params) == ("pipeline.preview", "queued", {"limit": 2})

    same = client.post("/api/v1/pipeline/runs", json={"dry_run": True, "limit": 2})
    assert same.status_code == 200 and same.json() == {"job_id": answer.json()["job_id"], "existing": True}
    other = client.post("/api/v1/pipeline/runs", json={"dry_run": True})  # other params: its own preview
    assert other.status_code == 202
    assert [j.type for j in _jobs(pg_engine)] == ["pipeline.run", "pipeline.preview", "pipeline.preview"]
    again = client.post("/api/v1/pipeline/runs")  # and a preview does not count as a run
    assert again.status_code == 200 and again.json()["job_id"] == run["job_id"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"limit": -1}, "limit must be a whole number of 0 or more, not -1"),
        ({"profile": "   "}, "profile must be a profile name, not '   '"),
        ({"dry_run": True, "limit": -1}, "limit must be a whole number of 0 or more, not -1"),
    ],
)
def test_bad_params_are_422_with_the_handler_message(client, pg_engine, body, message):
    answer = client.post("/api/v1/pipeline/runs", json=body)

    assert answer.status_code == 422
    assert answer.json() == {"detail": message}
    assert _jobs(pg_engine) == []


@pytest.mark.parametrize(
    "body",
    [
        {"only": ["x"]},  # an unknown field
        {"requeue": ["x"]},
        {"limit": "3"},  # no coercion
        {"limit": 1.5},
        {"limit": True},
        {"limit": 10**30},
        {"dry_run": "yes"},
        {"retry_deferred": 1},
        {"profile": 7},
        {"profile": "p" * 1000},
        [1, 2],
    ],
)
def test_hostile_bodies_are_422_and_enqueue_nothing(client, pg_engine, body):
    answer = client.post("/api/v1/pipeline/runs", json=body)

    assert answer.status_code == 422
    assert _jobs(pg_engine) == []


def test_a_body_that_is_not_json_is_422(client, pg_engine):
    answer = client.post(
        "/api/v1/pipeline/runs", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert answer.status_code == 422 and _jobs(pg_engine) == []


def test_publish_trigger_enqueues_publish_with_dedupe(client, pg_engine):
    first = client.post("/api/v1/pipeline/publish")

    assert first.status_code == 202
    assert set(first.json()) == {"job_id", "existing"} and first.json()["existing"] is False
    [job] = _jobs(pg_engine)
    assert (job.type, job.params, job.dedupe_key) == (
        "pipeline.publish",
        {"pull": True, "push": True},
        "api:pipeline.publish",
    )

    again = client.post("/api/v1/pipeline/publish")
    assert again.status_code == 200 and again.json() == {"job_id": first.json()["job_id"], "existing": True}
    assert client.post("/api/v1/pipeline/runs").status_code == 202  # a publish does not count as a run

    _set_status(pg_engine, first.json()["job_id"], "succeeded")
    with session_scope(pg_engine) as session:  # the scheduler's publish counts as existing
        scheduled, _ = enqueue(
            session,
            type="pipeline.publish",
            now=NOW,
            params={"pull": True, "push": True},
            dedupe_key="schedule:publish",
        )
        scheduled_id = str(scheduled.id)
    answer = client.post("/api/v1/pipeline/publish")
    assert answer.status_code == 200 and answer.json() == {"job_id": scheduled_id, "existing": True}
    assert client.post("/api/v1/pipeline/publish", json={"push": False}).status_code == 422  # takes no body


def test_requeue_enqueues_a_run_with_only_that_name_and_404s_an_unknown_item(client, pg_engine):
    with session_scope(pg_engine) as session:
        stage_item(
            session,
            calculated_name="notes/walks.md",
            doc_id="walks",
            doc_class="note",
            now=NOW,
            inbox_path=None,
            original_filename=None,
        )
    queued_run = client.post("/api/v1/pipeline/runs").json()  # a queued run does not stop a requeue

    answer = client.post("/api/v1/items/notes/walks.md/requeue")

    assert answer.status_code == 202
    assert set(answer.json()) == {"job_id", "existing"} and answer.json()["existing"] is False
    assert answer.json()["job_id"] != queued_run["job_id"]
    job = _jobs(pg_engine)[-1]
    assert (job.type, job.params, job.dedupe_key) == ("pipeline.run", {"requeue": ["notes/walks.md"]}, None)
    second = client.post("/api/v1/items/notes/walks.md/requeue")  # its own job each time
    assert second.status_code == 202 and second.json()["job_id"] != answer.json()["job_id"]

    unknown = client.post("/api/v1/items/notes/nothing.md/requeue")
    assert unknown.status_code == 404 and unknown.json() == {"detail": "item not found"}
    assert client.post("/api/v1/items/walks/requeue").status_code == 404  # the calculated name, exactly
    hostile = client.post("/api/v1/items/notes%00x.md/requeue")
    assert hostile.status_code == 422 and "not a document name" in hostile.json()["detail"]
    too_long = client.post("/api/v1/items/" + "n" * 600 + "/requeue")
    assert too_long.status_code == 422
    assert len(_jobs(pg_engine)) == 3


ENDPOINTS = ["/api/v1/pipeline/runs", "/api/v1/pipeline/publish", "/api/v1/items/notes/walks.md/requeue"]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_run_scope_is_required_and_read_does_not_imply_it(client, pg_engine, path):
    assert client.post(path, headers={"Authorization": ""}).status_code == 401
    assert (
        client.post(path, headers={"Authorization": "Bearer wrong-key-0123456789abcdefgh"}).status_code == 401
    )
    refused = client.post(path, headers={"Authorization": f"Bearer {READER}"})
    assert refused.status_code == 403 and refused.json() == {"detail": "this key does not have the run scope"}
    assert _jobs(pg_engine) == []
    # and `run` does not imply `read`
    assert client.get("/api/v1/jobs").status_code == 403
    assert client.get("/api/v1/jobs", headers={"Authorization": f"Bearer {BOTH}"}).status_code == 200


def test_post_does_not_wait_for_the_worker(client, pg_engine):
    for path in ENDPOINTS[:2]:
        answer = client.post(path)
        assert answer.status_code == 202
    answer = client.post("/api/v1/pipeline/runs", json={"dry_run": True})
    assert answer.status_code == 202

    jobs = _jobs(pg_engine)  # no worker runs here: every job is still queued, untouched
    assert len(jobs) == 3
    assert all(
        (j.status, j.attempts, j.started_at, j.locked_by, j.result) == ("queued", 0, None, None, None)
        for j in jobs
    )
