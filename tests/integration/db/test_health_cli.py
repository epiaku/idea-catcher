"""`catcher health`: does a worker hold the one-worker lock (real Postgres, CliRunner)."""

import pytest
from sqlalchemy import Engine
from typer.testing import CliRunner

from catcher.cli import app
from catcher.modules.worker.guard import WorkerLock, worker_running


@pytest.fixture
def runner(fresh_database_url: str, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)  # not upgraded: the lock needs no tables
    return CliRunner()


def test_health_is_0_while_a_worker_holds_the_lock(runner: CliRunner, pg_engine: Engine) -> None:
    with WorkerLock(pg_engine):  # another backend than the one health uses
        result = runner.invoke(app, ["health"])
    assert result.exit_code == 0, result.output
    assert "worker running" in result.output


def test_health_is_1_when_nobody_holds_the_lock(runner: CliRunner) -> None:
    result = runner.invoke(app, ["health"])
    assert result.exit_code == 1, result.output
    assert "no worker holds the lock" in result.output


def test_health_is_1_after_the_worker_lock_is_released(runner: CliRunner, pg_engine: Engine) -> None:
    with WorkerLock(pg_engine):
        assert runner.invoke(app, ["health"]).exit_code == 0
    assert runner.invoke(app, ["health"]).exit_code == 1


def test_health_takes_no_lock_and_a_worker_can_still_start_right_after(
    runner: CliRunner, pg_engine: Engine
) -> None:
    assert runner.invoke(app, ["health"]).exit_code == 1
    with WorkerLock(pg_engine):
        assert worker_running(pg_engine)


def test_health_does_not_see_a_different_advisory_key(runner: CliRunner, pg_engine: Engine) -> None:
    with WorkerLock(pg_engine, key=12345):
        result = runner.invoke(app, ["health"])
        assert worker_running(pg_engine, key=12345)
    assert result.exit_code == 1, result.output


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://catcher:s3cr3t-pw@127.0.0.1:1/catcher",
        "postgresql+psycopg://catcher:s3cr3t-pw@localhost:notaport/catcher",
        "not a database url s3cr3t-pw",
    ],
)
def test_health_exits_2_for_an_unreachable_database_and_a_malformed_url(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    result = runner.invoke(app, ["health"])
    assert result.exit_code == 2, result.output
    assert "s3cr3t-pw" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
