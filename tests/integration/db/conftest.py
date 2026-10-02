import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, text

from catcher.core.db import make_engine, session_scope

IMAGE = "pgvector/pgvector:pg17"


def _point_docker_at_the_desktop_socket() -> None:
    """Docker Desktop on a Mac keeps its socket under the home folder, not at /var/run."""
    if os.environ.get("DOCKER_HOST") or Path("/var/run/docker.sock").exists():
        return
    desktop = Path.home() / ".docker" / "run" / "docker.sock"
    if desktop.exists():
        os.environ["DOCKER_HOST"] = f"unix://{desktop}"


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    _point_docker_at_the_desktop_socket()
    try:
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer(IMAGE, driver="psycopg")
        container.start()
    except Exception as error:  # no daemon, no socket, no image: the DB tests cannot run here
        pytest.skip(f"Docker is not running ({type(error).__name__})")
    engine = make_engine(container.get_connection_url())
    try:
        yield engine  # Task 8 adds `alembic upgrade head` here
    finally:
        engine.dispose()
        container.stop()


@pytest.fixture
def session(pg_engine: Engine):
    with session_scope(pg_engine) as db_session:
        yield db_session
    tables = inspect(pg_engine).get_table_names(schema="public")
    if tables:
        names = ", ".join(f'"{name}"' for name in tables)
        with session_scope(pg_engine) as cleanup:
            cleanup.execute(text(f"truncate {names} restart identity cascade"))


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()
