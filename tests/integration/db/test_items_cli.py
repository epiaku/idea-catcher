"""`catcher items list` on a migrated fresh database (real Postgres, CliRunner): read-only, newest first."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.db import make_engine, session_scope
from catcher.modules.queue.items import stage_item
from catcher.modules.queue.models import JobItem
from catcher.modules.queue.states import ItemStates

T0 = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
LONG = "deferred for 3 days: the model is down\nand it said so " + "at length " * 20


@pytest.fixture
def runner(fresh_database_url: str, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    cli = CliRunner()
    result = cli.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    return cli


@pytest.fixture
def engine(fresh_database_url: str, runner: CliRunner):
    db = make_engine(fresh_database_url)
    try:
        yield db
    finally:
        db.dispose()


def _item(engine, name: str, doc_class: str, status: str, *, at: datetime, reason: str | None = None) -> None:
    with session_scope(engine) as session:
        stage_item(
            session,
            calculated_name=name,
            doc_id=name,
            doc_class=doc_class,
            now=at - timedelta(minutes=1),
            inbox_path=f"inbox/{name}",
            original_filename=name.rsplit("/", 1)[-1],
        )
        ItemStates().transition(session, name, status, now=at, reason=reason)


def _seed(engine) -> None:
    _item(engine, "notes/old.md", "note", "published", at=T0)
    _item(engine, "clippings/chat.md", "chat", "stuck", at=T0 + timedelta(days=1), reason=LONG)
    _item(
        engine, "clippings/clip.md", "youtube", "deferred", at=T0 + timedelta(days=2), reason="no transcript"
    )


def _local(at: datetime) -> str:
    return at.astimezone().strftime("%Y-%m-%d %H:%M")


def test_items_list_shows_stuck_items(runner: CliRunner, engine) -> None:
    _seed(engine)

    result = runner.invoke(app, ["items", "list"])

    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert [line.split()[0] for line in lines] == ["clippings/clip.md", "clippings/chat.md", "notes/old.md"]
    stuck = lines[1]
    assert stuck.split()[:3] == ["clippings/chat.md", "chat", "stuck"]
    assert _local(T0 + timedelta(days=1)) in stuck
    assert "deferred for 3 days: the model is down and it said so" in stuck  # on one line
    assert len(stuck.split(_local(T0 + timedelta(days=1)), 1)[1].strip()) <= 80  # the reason, shortened
    assert lines[0].endswith("no transcript")
    with session_scope(engine) as session:  # read-only
        assert {i.status for i in session.scalars(select(JobItem))} == {"published", "stuck", "deferred"}


def test_items_list_filters_by_status(runner: CliRunner, engine) -> None:
    _seed(engine)

    stuck = runner.invoke(app, ["items", "list", "--status", "stuck"])
    assert stuck.exit_code == 0, stuck.output
    [line] = stuck.output.strip().splitlines()
    assert line.startswith("clippings/chat.md")

    newest = runner.invoke(app, ["items", "list", "--limit", "1"])
    assert newest.exit_code == 0, newest.output
    [line] = newest.output.strip().splitlines()
    assert line.startswith("clippings/clip.md")

    none = runner.invoke(app, ["items", "list", "--status", "failed"])
    assert none.exit_code == 0, none.output
    assert none.output.strip() == ""


def test_items_list_rejects_a_bad_status(runner: CliRunner, engine) -> None:
    result = runner.invoke(app, ["items", "list", "--status", "lost"])
    assert result.exit_code == 2
    assert "stuck" in result.output  # the statuses it accepts are named


def test_items_list_exits_2_without_the_database(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://catcher:s3cr3t-pw@127.0.0.1:1/catcher")
    result = runner.invoke(app, ["items", "list"])
    assert result.exit_code == 2, result.output
    assert result.output.count("cannot reach the database in DATABASE_URL") == 1
    assert "s3cr3t-pw" not in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
