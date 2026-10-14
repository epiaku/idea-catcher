"""`POST /backfill/release` (`run` scope): it enqueues one `backfill.release` and answers; a queued or
running one is answered `200` whatever its `limit`; two requests at once make one."""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select

from catcher.api import routes_runs
from catcher.api.app import create_app
from catcher.core.config import Settings
from catcher.core.db import session_scope
from catcher.modules.queue.models import Job

pytestmark = pytest.mark.db

READER = "reader-key-0123456789abcdefghij"
RUNNER = "runner-key-0123456789abcdefghij"
RELEASE = "/api/v1/backfill/release"


@pytest.fixture
def client(pg_engine: Engine) -> Iterator[TestClient]:
    settings = Settings(
        database_url=pg_engine.url.render_as_string(hide_password=False),
        api_keys=SecretStr(f"reader:read:{READER} runner:run:{RUNNER}"),
    )
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {RUNNER}"}) as test_client:
        yield test_client


def _jobs(engine: Engine) -> list[Job]:
    with session_scope(engine) as session:
        return list(session.scalars(select(Job).order_by(Job.created_at, Job.id)))


def test_post_release_enqueues_backfill_release_with_202(client, pg_engine):
    answer = client.post(RELEASE)

    assert answer.status_code == 202
    body = answer.json()
    assert set(body) == {"job_id", "existing"} and body["existing"] is False
    [job] = _jobs(pg_engine)
    assert (str(job.id), job.type, job.status) == (body["job_id"], "backfill.release", "queued")
    assert job.resource is None and job.priority == 0
    assert job.dedupe_key == "api:backfill.release"


def test_limit_is_stored_when_given_and_absent_when_omitted_or_null(client, pg_engine):
    for body, expected in [({"limit": 7}, {"limit": 7}), ({}, {}), ({"limit": None}, {})]:
        assert client.post(RELEASE, json=body).status_code == 202
        [job] = _jobs(pg_engine)
        assert dict(job.params) == expected
        if expected:
            assert type(job.params["limit"]) is int
        with session_scope(pg_engine) as session:
            session.delete(session.get(Job, job.id))


def test_second_post_is_200_with_the_same_job_id_whatever_the_limit(client, pg_engine):
    first = client.post(RELEASE, json={"limit": 3}).json()

    again = client.post(RELEASE, json={"limit": 50})
    assert again.status_code == 200
    assert again.json() == {"job_id": first["job_id"], "existing": True}
    assert client.post(RELEASE).json() == {"job_id": first["job_id"], "existing": True}
    [job] = _jobs(pg_engine)
    assert dict(job.params) == {"limit": 3}


def test_two_posts_at_once_make_one_job(client, pg_engine, monkeypatch):
    barrier = threading.Barrier(2)
    looked_up = routes_runs.active_job

    def both_look_first(*args):
        found = looked_up(*args)
        barrier.wait(timeout=10)  # both requests found nothing before either inserts
        return found

    monkeypatch.setattr(routes_runs, "active_job", both_look_first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda _: client.post(RELEASE), range(2)))

    assert sorted(a.status_code for a in answers) == [200, 202]
    assert answers[0].json()["job_id"] == answers[1].json()["job_id"]
    assert len(_jobs(pg_engine)) == 1


@pytest.mark.parametrize("limit", [0, 101, -1, True, "5", 1.5])
def test_limit_0_101_negative_true_string_float_are_422(client, pg_engine, limit):
    assert client.post(RELEASE, json={"limit": limit}).status_code == 422
    assert _jobs(pg_engine) == []


def test_unknown_field_is_422(client, pg_engine):
    assert client.post(RELEASE, json={"unknown": 1}).status_code == 422
    assert _jobs(pg_engine) == []


def test_reader_key_is_403_and_no_key_is_401(client, pg_engine):
    assert client.post(RELEASE, headers={"Authorization": f"Bearer {READER}"}).status_code == 403
    assert client.post(RELEASE, headers={"Authorization": ""}).status_code == 401
    assert _jobs(pg_engine) == []


def test_openapi_lists_the_release_path(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths[RELEASE]
