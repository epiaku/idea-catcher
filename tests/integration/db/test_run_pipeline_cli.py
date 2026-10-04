"""`catcher run pipeline` takes the worker's Postgres lock for the whole run (real Postgres, CliRunner).

With the database down, or with a worker (or another run) holding the lock, it exits 2 before it touches a
file; when the lock is lost during the run, it stops with exit 1 before the next document."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.db import make_engine
from catcher.modules.pipeline import run as run_mod
from catcher.modules.worker.guard import WorkerLock

pytestmark = pytest.mark.db

NOTES = "hugo/content/en/docs/idea-bucket/notes"
CLOSED_DATABASE = "postgresql+psycopg://catcher:s3cr3t-pw@127.0.0.1:1/catcher"  # nothing listens on port 1


@pytest.fixture
def repos(make_repo):
    _, ideas = make_repo(
        "idea-bucket",
        {"inbox/notes/first idea.md": "A first idea\n", "inbox/notes/second idea.md": "A second idea\n"},
    )
    _, docs = make_repo("epiaku-docs", {f"{NOTES}/_index.md": "---\ntitle: Notes\n---\n"})
    return SimpleNamespace(ideas=ideas, docs=docs)


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


def _args(repos, *extra: str) -> list[str]:
    return ["run", "pipeline", "--ideas", str(repos.ideas), "--docs", str(repos.docs), *extra]


def _inbox(ideas: Path) -> list[str]:
    return sorted(p.name for p in (ideas / "inbox").rglob("*.md"))


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if ".git" not in p.parts)


def _advisory_locks(engine: Engine) -> list[int]:
    """The backends that hold an advisory lock in this test's database."""
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "select pid from pg_locks where locktype = 'advisory' and granted"
                    " and database = (select oid from pg_database where datname = current_database())"
                )
            ).scalars()
        )


def test_run_pipeline_exits_2_when_the_database_is_down_and_changes_nothing(
    runner: CliRunner, repos, monkeypatch: pytest.MonkeyPatch, sh
) -> None:
    monkeypatch.setenv("DATABASE_URL", CLOSED_DATABASE)
    before = (_tree(repos.ideas), _tree(repos.docs), sh(repos.ideas, "log", "--oneline"))
    result = runner.invoke(app, _args(repos))
    assert result.exit_code == 2, result.output
    assert "cannot reach the database in DATABASE_URL" in result.output
    assert CLOSED_DATABASE not in result.output and "s3cr3t-pw" not in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert (_tree(repos.ideas), _tree(repos.docs), sh(repos.ideas, "log", "--oneline")) == before


def test_run_pipeline_exits_2_on_a_malformed_database_url_without_the_password(
    runner: CliRunner, repos, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://catcher:s3cr3t-pw@localhost:notaport/catcher")
    before = _tree(repos.ideas)
    result = runner.invoke(app, _args(repos))
    assert result.exit_code == 2, result.output
    assert "DATABASE_URL is not a valid database URL" in result.output
    assert "s3cr3t-pw" not in result.output
    assert _tree(repos.ideas) == before


def test_dry_run_also_needs_the_database(runner: CliRunner, repos, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", CLOSED_DATABASE)
    result = runner.invoke(app, _args(repos, "--dry-run"))
    assert result.exit_code == 2, result.output
    assert "cannot reach the database in DATABASE_URL" in result.output
    assert "would_publish" not in result.output  # it did not run


def test_run_pipeline_exits_2_while_a_worker_holds_the_lock(runner: CliRunner, repos, engine, sh) -> None:
    before = (_tree(repos.ideas), _tree(repos.docs), sh(repos.ideas, "log", "--oneline"))
    with WorkerLock(engine):  # a worker runs
        result = runner.invoke(app, _args(repos))
        dry = runner.invoke(app, _args(repos, "--dry-run"))
    for refused in (result, dry):
        assert refused.exit_code == 2, refused.output
        assert "another worker or run is already running; one at a time" in refused.output
        assert refused.exception is None or isinstance(refused.exception, SystemExit)
    assert (_tree(repos.ideas), _tree(repos.docs), sh(repos.ideas, "log", "--oneline")) == before


def test_run_pipeline_takes_and_releases_the_lock(
    runner: CliRunner, repos, engine, make_services, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_during_the_run: list[int] = []

    def services(settings):
        held_during_the_run.extend(_advisory_locks(engine))
        return make_services()

    monkeypatch.setattr("catcher.cli.default_services", services)
    first = runner.invoke(app, _args(repos, "--limit", "1"))
    assert first.exit_code == 0, first.output
    assert len(held_during_the_run) == 1  # the run held the lock
    assert _advisory_locks(engine) == []  # and let it go at the end
    second = runner.invoke(app, _args(repos))  # a second run works after the first
    assert second.exit_code == 0, second.output
    assert "published" in second.output
    assert _inbox(repos.ideas) == []
    with WorkerLock(engine):  # and a worker can take the lock after it
        pass


def test_a_run_that_loses_its_lock_stops_with_exit_1_before_the_next_document(
    runner: CliRunner, repos, engine, sh, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_process_note = run_mod.process_note

    def process_and_lose_the_lock(note, svc, opts):
        processed = real_process_note(note, svc, opts)
        [pid] = _advisory_locks(engine)  # a Postgres restart, or a dropped connection, ends the lock
        with engine.connect() as admin:
            assert admin.execute(text("select pg_terminate_backend(:pid, 5000)"), {"pid": pid}).scalar()
        return processed

    monkeypatch.setattr(run_mod, "process_note", process_and_lose_the_lock)
    commits = sh(repos.ideas, "log", "--oneline")
    result = runner.invoke(app, _args(repos))
    assert result.exit_code == 1, result.output
    assert "the run lost its database lock" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert len(_inbox(repos.ideas)) == 1  # the second document was not started
    assert sh(repos.ideas, "log", "--oneline") == commits  # nothing was committed after the lock was lost
    assert sh(repos.ideas, "status", "--porcelain") != ""  # the first one is changed, not committed
    assert any(line.startswith("published") for line in result.output.splitlines())  # what was done
    assert "1 document(s) were finished and are NOT committed" in result.output
