"""`catcher worker` and `catcher jobs` on a migrated fresh database (real Postgres, CliRunner)."""

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner

from catcher import cli
from catcher.cli import app
from catcher.core.db import make_engine, session_scope
from catcher.modules.queue.models import Job
from catcher.modules.worker import app as worker_app
from catcher.modules.worker.guard import WorkerLock
from catcher.modules.worker.handlers import Done, HandlerContext


@pytest.fixture
def runner(fresh_database_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CliRunner:
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    monkeypatch.setenv("CATCHER_STATE_DIR", str(tmp_path / "state"))  # never the real YouTube gate
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


def _jobs(engine) -> list[Job]:
    with session_scope(engine) as session:
        return list(session.scalars(select(Job).order_by(Job.created_at)))


def test_jobs_add_enqueues_and_prints_the_id(runner: CliRunner, engine) -> None:
    result = runner.invoke(
        app,
        [
            "jobs", "add", "pipeline.run",
            "--param", "retry_deferred=true",
            "--param", "limit=5",
            "--param", "profile=notes",
            "--param", "refresh_llm=False",
            "--priority", "3",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    [job] = _jobs(engine)
    assert result.output.strip() == str(job.id)
    assert job.type == "pipeline.run"
    assert job.status == "queued"
    assert job.priority == 3
    assert job.params == {"retry_deferred": True, "limit": 5, "profile": "notes", "refresh_llm": False}


@pytest.mark.parametrize("param", ["dry_run=true", "dry-run=true", "Dry_Run=false", "DRYRUN=1"])
def test_jobs_add_refuses_dry_run(runner: CliRunner, engine, param: str) -> None:
    result = runner.invoke(app, ["jobs", "add", "pipeline.run", "--param", param])
    assert result.exit_code == 2
    assert "dry run" in result.output.lower()
    assert "queue" in result.output.lower()
    assert _jobs(engine) == []


def test_jobs_add_refuses_a_param_without_a_value(runner: CliRunner, engine) -> None:
    result = runner.invoke(app, ["jobs", "add", "pipeline.run", "--param", "limit"])
    assert result.exit_code == 2
    assert _jobs(engine) == []


def test_jobs_list_filters_by_status(runner: CliRunner, engine) -> None:
    ids = []
    for job_type in ("pipeline.run", "youtube.fetch", "llm.reason"):
        result = runner.invoke(app, ["jobs", "add", job_type])
        assert result.exit_code == 0, result.output
        ids.append(result.output.strip())
    with session_scope(engine) as session:
        session.execute(
            update(Job)
            .where(Job.id == uuid.UUID(ids[1]))
            .values(status="failed", error="no handler for youtube.fetch")
        )

    everything = runner.invoke(app, ["jobs", "list"])
    assert everything.exit_code == 0, everything.output
    assert len(everything.output.strip().splitlines()) == 3
    assert all(job_id in everything.output for job_id in ids)

    failed = runner.invoke(app, ["jobs", "list", "--status", "failed"])
    assert failed.exit_code == 0, failed.output
    [line] = failed.output.strip().splitlines()
    assert line.split()[:3] == [ids[1], "youtube.fetch", "failed"]
    assert "no handler for youtube.fetch" in line

    queued = runner.invoke(app, ["jobs", "list", "--status", "queued", "--limit", "1"])
    assert queued.exit_code == 0, queued.output
    assert len(queued.output.strip().splitlines()) == 1

    bad = runner.invoke(app, ["jobs", "list", "--status", "lost"])
    assert bad.exit_code == 2


def test_worker_once_runs_a_registered_handler_and_exits(
    runner: CliRunner, engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[tuple[dict, Path, Path]] = []

    def echo(ctx: HandlerContext, job: Job) -> Done:
        seen.append((dict(job.params), ctx.ideas, ctx.docs))
        return Done({"echo": job.params.get("word")})

    monkeypatch.setitem(worker_app.EXTRA_HANDLERS, "test.echo", echo)
    for word in ("one", "two"):
        assert runner.invoke(app, ["jobs", "add", "test.echo", "--param", f"word={word}"]).exit_code == 0
    assert runner.invoke(app, ["jobs", "add", "test.unknown"]).exit_code == 0

    ideas, docs = tmp_path / "ideas", tmp_path / "docs"
    result = runner.invoke(app, ["worker", "--once", "--ideas", str(ideas), "--docs", str(docs)])
    assert result.exit_code == 0, result.output
    assert sorted(params["word"] for params, _, _ in seen) == ["one", "two"]
    assert {(i, d) for _, i, d in seen} == {(ideas, docs)}
    by_type = {}
    for job in _jobs(engine):
        by_type.setdefault(job.type, []).append(job)
    assert [j.status for j in by_type["test.echo"]] == ["succeeded", "succeeded"]
    assert sorted(j.result["echo"] for j in by_type["test.echo"]) == ["one", "two"]  # type: ignore[index]
    [unknown] = by_type["test.unknown"]
    assert unknown.status == "failed"
    assert unknown.error == "no handler for test.unknown"
    assert "succeeded=2" in result.output and "failed=1" in result.output

    # The lock was released: a second run starts, finds nothing, and exits.
    again = runner.invoke(app, ["worker", "--once", "--ideas", str(ideas), "--docs", str(docs)])
    assert again.exit_code == 0, again.output


def test_a_second_worker_command_is_refused_with_exit_2(runner: CliRunner, engine) -> None:
    assert runner.invoke(app, ["jobs", "add", "test.echo"]).exit_code == 0
    with WorkerLock(engine):
        result = runner.invoke(app, ["worker", "--once"])
    assert result.exit_code == 2
    assert "another worker is already running" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    [job] = _jobs(engine)
    assert job.status == "queued"  # the refused worker claimed nothing


@pytest.mark.parametrize("option", ["--lease-s", "--poll-s"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_worker_refuses_a_bad_interval(runner: CliRunner, option: str, value: str) -> None:
    result = runner.invoke(app, ["worker", "--once", option, value])
    assert result.exit_code == 2
    assert option in result.output


def test_a_signal_sets_stop_and_the_previous_handlers_come_back(runner: CliRunner) -> None:
    before = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    stop = threading.Event()
    with cli._stop_on_signals(stop):
        os.kill(os.getpid(), signal.SIGTERM)
        assert stop.wait(5)
        with pytest.raises(KeyboardInterrupt):  # a second signal does not wait for the job
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(5)
    assert {signum: signal.getsignal(signum) for signum in before} == before
    assert runner.invoke(app, ["worker", "--once"]).exit_code == 0
    assert {signum: signal.getsignal(signum) for signum in before} == before


def test_an_idle_worker_process_stops_at_once_on_sigterm(
    runner: CliRunner, fresh_database_url: str, tmp_path: Path
) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": fresh_database_url,
        "CATCHER_STATE_DIR": str(tmp_path / "state"),
        "LOG_FILE": str(tmp_path / "worker.log"),
        "LOG_LEVEL": "INFO",
    }
    command = [sys.executable, "-c", "from catcher.cli import app; app()", "worker", "--poll-s", "3600"]
    process = subprocess.Popen(command, env=env, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stderr is not None
        for line in process.stderr:  # wait until it holds the lock and runs
            if "worker" in line and "started" in line:
                break
        else:
            pytest.fail(f"the worker never started (exit {process.wait(5)})")
        time.sleep(0.5)  # into the idle wait (poll 3600 s)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
