import time

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine

from catcher.api.app import create_app
from catcher.core.config import Settings
from catcher.modules.worker.guard import WorkerLock

pytestmark = pytest.mark.db

KEY = "health-key-0123456789abcdefghij"
DEAD_DATABASE = "postgresql+psycopg://catcher:catcher@127.0.0.1:1/catcher"  # nobody listens on port 1


def _settings(database_url: str) -> Settings:
    return Settings(database_url=database_url, api_keys=SecretStr(f"ops:read:{KEY}"), api_docs=True)


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def test_the_health_endpoint_needs_no_key_and_reports_database_and_worker(pg_engine, worker_engine):
    with WorkerLock(worker_engine), TestClient(create_app(_settings(_url(pg_engine)))) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"api": "ok", "database": "ok", "worker": "running"}


def test_health_is_503_naming_the_failing_part_when_no_worker_holds_the_lock(pg_engine):
    with TestClient(create_app(_settings(_url(pg_engine)))) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"api": "ok", "database": "ok", "worker": "none"}


def test_health_is_503_when_the_database_is_down():
    with TestClient(create_app(_settings(DEAD_DATABASE))) as client:
        started = time.monotonic()
        response = client.get("/health")
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json() == {"api": "ok", "database": "down", "worker": "unknown"}
    assert "catcher:catcher" not in response.text and "127.0.0.1" not in response.text
    assert elapsed < 10
