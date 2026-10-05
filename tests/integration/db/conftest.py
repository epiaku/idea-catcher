import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import make_url
from worker_harness import FrozenClock, FrozenHarness, WorkerHarness, seeded_harness
from worker_harness import frozen_harness as build_frozen_harness

from catcher.core.db import alembic_config, make_engine, make_worker_engine, session_scope

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


def seed_open_youtube_row(engine: Engine) -> None:
    """The row migration 0004 seeds; a missing row means CLOSED, so a test that wants that deletes it."""
    with session_scope(engine) as seeding:
        seeding.execute(
            text(
                "insert into resources (name, streak, concurrency, updated_at)"
                " values ('youtube', 0, 1, :at) on conflict (name) do nothing"
            ),
            {"at": datetime(2026, 10, 4, tzinfo=UTC)},
        )


@pytest.fixture(autouse=True)
def _empty_tables(pg_engine: Engine) -> None:
    """Start every test on empty tables, also those that only use `pg_engine` and commit rows of their own.

    Skips with `pg_engine` when Docker is not available."""
    _truncate_all(pg_engine)
    seed_open_youtube_row(pg_engine)


@pytest.fixture
def session(pg_engine: Engine):
    with session_scope(pg_engine) as db_session:
        yield db_session
    _truncate_all(pg_engine)
    seed_open_youtube_row(pg_engine)


@pytest.fixture
def clock() -> FrozenClock:
    """An aware UTC clock frozen at 2026-10-02 12:00 that moves only with `advance(seconds)`."""
    return FrozenClock()


@pytest.fixture
def worker_engine(pg_engine: Engine) -> Iterator[Engine]:
    """An engine like the worker's (a lock timeout on every session), on the migrated test database."""
    engine = make_worker_engine(pg_engine.url.render_as_string(hide_password=False))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def harness(make_repo, worker_engine, make_services, yt_facts) -> WorkerHarness:
    """A worker on the test_run seed (a note, a Gemini chat, a YouTube clip); see worker_harness.py."""
    return seeded_harness(
        make_repo=make_repo,
        engine=worker_engine,
        services=make_services(),
        yt_facts=yt_facts,
    )


@pytest.fixture
def frozen_harness(worker_engine, make_services, tmp_path) -> FrozenHarness:
    """A worker on a copy of the committed tests/data; the model and YouTube record the attempt and raise."""
    return build_frozen_harness(target=tmp_path / "ic", engine=worker_engine, services=make_services())
