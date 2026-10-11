"""`catcher schedules` on a migrated fresh database (real Postgres, CliRunner): read-only."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from catcher import cli
from catcher.cli import app
from catcher.core.db import make_engine, session_scope, utc_now
from catcher.modules.queue.models import Job, Schedule


@pytest.fixture
def runner(fresh_database_url: str, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    for name in ("IDEAS_PULL", "PIPELINE_RUN", "PUBLISH"):  # the last two are removed: they do nothing
        monkeypatch.setenv(f"SCHEDULE_{name}", "")
    cli_runner = CliRunner()
    result = cli_runner.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    return cli_runner


@pytest.fixture
def engine(fresh_database_url: str, runner: CliRunner):
    db = make_engine(fresh_database_url)
    try:
        yield db
    finally:
        db.dispose()


def test_schedules_lists_the_one_with_last_fired_and_next_due(
    runner: CliRunner, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 7, 12, 0, tzinfo=utc_now().tzinfo)  # 14:00 in Amsterdam (CEST)
    monkeypatch.setattr(cli, "utc_now", lambda: now)
    monkeypatch.setenv("SCHEDULE_IDEAS_PULL", "0 * * * *")
    monkeypatch.setenv("SCHEDULE_PIPELINE_RUN", "0 8 * * *")  # removed: not listed
    with session_scope(engine) as session:
        session.add(Schedule(name="ideas_pull", last_fired_at=now - timedelta(hours=1)))
    result = runner.invoke(app, ["schedules"])
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.output.splitlines()[1:]}
    assert result.output.splitlines()[0].split() == [
        "name",
        "cron",
        "timezone",
        "last",
        "fired",
        "next",
        "due",
    ]
    assert set(lines) == {"ideas_pull"}
    assert "0 * * * *" in lines["ideas_pull"]
    assert "Europe/Amsterdam" in lines["ideas_pull"]
    assert "2026-10-07 11:00" in lines["ideas_pull"]  # last fired (UTC)
    assert "due now" in lines["ideas_pull"]  # the 13:00 and 14:00 Amsterdam slots have passed since then


def test_schedules_shows_off_for_an_empty_variable(runner: CliRunner, engine) -> None:
    result = runner.invoke(app, ["schedules"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()[1:]
    assert len(lines) == 1
    assert all("off" in line for line in lines)
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(Schedule)) == 0  # read-only
        assert session.scalar(select(func.count()).select_from(Job)) == 0
