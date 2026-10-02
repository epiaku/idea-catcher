import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import make_url

from catcher.core.db import alembic_config, make_engine, session_scope

IMAGE = "pgvector/pgvector:pg17"


def _point_docker_at_the_desktop_socket() -> None:
    """Docker Desktop on a Mac keeps its socket under the home folder, not at /var/run."""
    if os.environ.get("DOCKER_HOST") or Path("/var/run/docker.sock").exists():
        return
    desktop = Path.home() / ".docker" / "run" / "docker.sock"
    if desktop.exists():
        os.environ["DOCKER_HOST"] = f"unix://{desktop}"


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    _point_docker_at_the_desktop_socket()
    try:
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer(IMAGE, driver="psycopg")
        container.start()
    except Exception as error:  # no daemon, no socket, no image: the DB tests cannot run here
        pytest.skip(f"Docker is not running ({type(error).__name__})")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture(scope="session")
def pg_engine(pg_url: str) -> Iterator[Engine]:
    command.upgrade(alembic_config(pg_url), "head")
    engine = make_engine(pg_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def fresh_database_url(pg_url: str) -> Iterator[str]:
    """A new, empty database in the same container, so migration tests leave the shared schema alone."""
    name = f"fresh_{uuid.uuid4().hex[:12]}"
    admin = make_engine(pg_url).execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'create database "{name}"'))
    try:
        yield make_url(pg_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'drop database if exists "{name}" with (force)'))
        admin.dispose()


def _truncate_all(engine: Engine) -> None:
    tables = [t for t in inspect(engine).get_table_names(schema="public") if t != "alembic_version"]
    if tables:
        names = ", ".join(f'"{name}"' for name in tables)
        with session_scope(engine) as cleanup:
            cleanup.execute(text(f"truncate {names} restart identity cascade"))


@pytest.fixture(autouse=True)
def _empty_tables(pg_engine: Engine) -> None:
    """Start every test on empty tables, also those that only use `pg_engine` and commit rows of their own.

    Skips with `pg_engine` when Docker is not available."""
    _truncate_all(pg_engine)


@pytest.fixture
def session(pg_engine: Engine):
    with session_scope(pg_engine) as db_session:
        yield db_session
    _truncate_all(pg_engine)


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
