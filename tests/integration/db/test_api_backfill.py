"""The backfill trigger endpoints (`run` scope): they enqueue `backfill.docs` and one `backfill.channel` per
URL and answer; the worker does the work. An active job is answered `200`; two requests at once make one."""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select

from catcher.api import routes_backfill
from catcher.api.app import create_app
from catcher.core.config import Settings
from catcher.core.db import session_scope
from catcher.modules.queue.models import Job

pytestmark = pytest.mark.db

READER = "reader-key-0123456789abcdefghij"
RUNNER = "runner-key-0123456789abcdefghij"
DOCS = "/api/v1/backfill/docs"
CHANNELS = "/api/v1/backfill/channels"
CHANNEL = "https://www.youtube.com/@somechannel"
OTHER = "https://www.youtube.com/@otherchannel"


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


def test_reader_key_gets_403_on_both_posts(client, pg_engine):
    headers = {"Authorization": f"Bearer {READER}"}
    assert client.post(DOCS, headers=headers).status_code == 403
    assert client.post(CHANNELS, json={"urls": [CHANNEL]}, headers=headers).status_code == 403
    assert _jobs(pg_engine) == []


def test_no_key_is_401(client):
    assert client.post(DOCS, headers={"Authorization": ""}).status_code == 401
    assert client.post(CHANNELS, json={"urls": [CHANNEL]}, headers={"Authorization": ""}).status_code == 401


def test_post_docs_enqueues_backfill_docs_with_202(client, pg_engine):
    answer = client.post(DOCS, json={"release": 5})

    assert answer.status_code == 202
    body = answer.json()
    assert set(body) == {"job_id", "existing"} and body["existing"] is False
    [job] = _jobs(pg_engine)
    assert (str(job.id), job.type, job.status) == (body["job_id"], "backfill.docs", "queued")
    assert job.params == {"dry_run": False, "pull": True, "release": 5}
    assert job.dedupe_key == "api:backfill.docs"


def test_second_post_docs_is_200_with_the_same_job_id(client, pg_engine):
    first = client.post(DOCS).json()

    again = client.post(DOCS, json={"pull": False})

    assert again.status_code == 200
    assert again.json() == {"job_id": first["job_id"], "existing": True}
    assert len(_jobs(pg_engine)) == 1


def test_dry_run_is_its_own_job(client, pg_engine):
    real = client.post(DOCS).json()

    dry = client.post(DOCS, json={"dry_run": True})
    assert dry.status_code == 202 and dry.json()["job_id"] != real["job_id"]
    dry_again = client.post(DOCS, json={"dry_run": True})
    assert dry_again.status_code == 200 and dry_again.json()["job_id"] == dry.json()["job_id"]
    real_again = client.post(DOCS)
    assert real_again.status_code == 200 and real_again.json()["job_id"] == real["job_id"]
    assert [j.params["dry_run"] for j in _jobs(pg_engine)] == [False, True]


@pytest.mark.parametrize(
    "body",
    [{"nope": 1}, {"release": -1}, {"release": "3"}, {"release": 2**31}, {"dry_run": "yes"}, {"pull": 1}],
)
def test_bad_docs_bodies_are_422(client, pg_engine, body):
    assert client.post(DOCS, json=body).status_code == 422
    assert _jobs(pg_engine) == []


def test_unknown_field_is_422(client):
    assert client.post(DOCS, json={"unknown": True}).status_code == 422


def test_negative_release_is_422(client):
    assert client.post(DOCS, json={"release": -1}).status_code == 422


def test_string_release_is_422(client):
    assert client.post(DOCS, json={"release": "3"}).status_code == 422


def test_post_channels_enqueues_one_job_per_url_at_backfill_priority_and_with_the_youtube_resource(
    client, pg_engine
):
    answer = client.post(CHANNELS, json={"urls": [CHANNEL, OTHER], "max_videos": 20, "release": 2})

    assert answer.status_code == 202
    jobs = answer.json()["jobs"]
    assert [(j["url"], j["existing"]) for j in jobs] == [(CHANNEL, False), (OTHER, False)]
    stored = _jobs(pg_engine)
    assert [str(j.id) for j in stored] == [j["job_id"] for j in jobs]
    for job, url in zip(stored, [CHANNEL, OTHER], strict=True):
        assert job.type == "backfill.channel" and job.status == "queued"
        assert job.params == {"url": url, "max_videos": 20, "release": 2}
        assert job.priority == -10
        assert job.resource == "youtube"
        assert job.dedupe_key and job.dedupe_key.startswith("api:backfill.channel:")
    assert stored[0].dedupe_key != stored[1].dedupe_key


def test_same_url_twice_returns_the_existing_job_with_200(client, pg_engine):
    first = client.post(CHANNELS, json={"urls": [CHANNEL]}).json()["jobs"][0]

    again = client.post(CHANNELS, json={"urls": [CHANNEL]})
    assert again.status_code == 200
    assert again.json()["jobs"] == [{"url": CHANNEL, "job_id": first["job_id"], "existing": True}]

    mixed = client.post(CHANNELS, json={"urls": [CHANNEL, OTHER]})  # one new: 202
    assert mixed.status_code == 202
    assert [j["existing"] for j in mixed.json()["jobs"]] == [True, False]
    assert len(_jobs(pg_engine)) == 2


def test_a_video_url_is_422_and_names_the_url_and_nothing_is_enqueued(client, pg_engine):
    video = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    answer = client.post(CHANNELS, json={"urls": [CHANNEL, video]})

    assert answer.status_code == 422
    assert video in str(answer.json()["detail"])
    assert _jobs(pg_engine) == []


def test_more_than_10_urls_is_422(client, pg_engine):
    urls = [f"https://www.youtube.com/@channel{n}" for n in range(11)]
    assert client.post(CHANNELS, json={"urls": urls}).status_code == 422
    assert client.post(CHANNELS, json={"urls": []}).status_code == 422
    assert _jobs(pg_engine) == []


@pytest.mark.parametrize("extra", [{"max_videos": 101}, {"max_videos": 0}, {"max_videos": "5"}, {"x": 1}])
def test_bad_channel_bodies_are_422(client, pg_engine, extra):
    assert client.post(CHANNELS, json={"urls": [CHANNEL], **extra}).status_code == 422
    assert _jobs(pg_engine) == []


def test_max_videos_101_is_422(client):
    assert client.post(CHANNELS, json={"urls": [CHANNEL], "max_videos": 101}).status_code == 422


def test_two_posts_at_once_make_one_job(client, pg_engine, monkeypatch):
    barrier = threading.Barrier(2)
    looked_up = routes_backfill.active_channel_job

    def both_look_first(*args):
        found = looked_up(*args)
        barrier.wait(timeout=10)  # both requests found nothing before either inserts
        return found

    monkeypatch.setattr(routes_backfill, "active_channel_job", both_look_first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda _: client.post(CHANNELS, json={"urls": [CHANNEL]}), range(2)))

    assert sorted(a.status_code for a in answers) == [200, 202]
    assert answers[0].json()["jobs"][0]["job_id"] == answers[1].json()["jobs"][0]["job_id"]
    assert len(_jobs(pg_engine)) == 1


def test_openapi_lists_the_new_paths(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths[DOCS] and "post" in paths[CHANNELS]
    assert "get" in paths["/api/v1/backfill"]
