"""`catcher render` writes a page into the docs checkout, so it takes the same Postgres lock as `run pipeline`
and the worker (real Postgres, CliRunner): a worker's `pipeline.publish` must never commit a preview page."""

from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.db import make_engine
from catcher.modules.pipeline.doctypes import destination_dir
from catcher.modules.worker.guard import WorkerLock

pytestmark = pytest.mark.db

CLOSED_DATABASE = "postgresql+psycopg://catcher:s3cr3t-pw@127.0.0.1:1/catcher"  # nothing listens on port 1


@pytest.fixture
def engine(fresh_database_url: str) -> Engine:
    db = make_engine(fresh_database_url)
    try:
        yield db  # type: ignore[misc]
    finally:
        db.dispose()


@pytest.fixture
def runner(fresh_database_url: str, monkeypatch: pytest.MonkeyPatch, make_services) -> CliRunner:
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    monkeypatch.setattr("catcher.cli.default_services", lambda settings: make_services())  # no real LLM
    return CliRunner()


def _advisory_locks(engine: Engine) -> list[int]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "select pid from pg_locks where locktype = 'advisory' and granted"
                    " and database = (select oid from pg_database where datname = current_database())"
                )
            ).scalars()
        )


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) if root.exists() else []


def _render(note_path: Path, docs: Path) -> list[str]:
    return ["render", str(note_path), "--docs", str(docs), "--profile", "fake"]


def test_render_exits_2_while_a_worker_holds_the_lock_and_writes_nothing(
    runner: CliRunner, engine, make_note, tmp_path: Path
) -> None:
    note = make_note("note", root=tmp_path / "ideas")
    docs = tmp_path / "docs"
    before = (_files(tmp_path / "ideas"), _files(docs))
    with WorkerLock(engine):  # a worker runs
        result = runner.invoke(app, _render(note.path, docs))
    assert result.exit_code == 2, result.output
    assert "another worker or run is already running; one at a time: nothing was done" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert (_files(tmp_path / "ideas"), _files(docs)) == before


def test_render_exits_2_when_the_database_is_down(
    runner: CliRunner, make_note, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", CLOSED_DATABASE)
    note = make_note("note", root=tmp_path / "ideas")
    docs = tmp_path / "docs"
    result = runner.invoke(app, _render(note.path, docs))
    assert result.exit_code == 2, result.output
    assert "cannot reach the database in DATABASE_URL: nothing was done" in result.output
    assert "s3cr3t-pw" not in result.output and "Traceback" not in result.output
    assert not docs.exists()


def test_render_holds_the_lock_while_it_works_and_lets_it_go(
    runner: CliRunner, engine, make_note, make_services, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_while_rendering: list[int] = []

    def services(settings):
        held_while_rendering.extend(_advisory_locks(engine))
        return make_services()

    monkeypatch.setattr("catcher.cli.default_services", services)
    note = make_note("note", root=tmp_path / "ideas")
    docs = tmp_path / "docs"
    result = runner.invoke(app, _render(note.path, docs))
    assert result.exit_code == 0, result.output
    assert len(list((docs / destination_dir("notes")).glob("*-a7b2c9.md"))) == 1
    assert len(held_while_rendering) == 1  # render held the lock
    assert _advisory_locks(engine) == []  # and let it go at the end
    with WorkerLock(engine):  # a worker can take it after
        pass
