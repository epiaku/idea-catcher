"""`catcher worker` and `catcher jobs` on a migrated fresh database (real Postgres, CliRunner)."""

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from catcher import __version__, cli
from catcher.cli import app
from catcher.core.db import make_engine, session_scope, utc_now
from catcher.modules.queue import queue
from catcher.modules.queue.models import Job
from catcher.modules.queue.queue import enqueue
from catcher.modules.worker import app as worker_app
from catcher.modules.worker import loop
from catcher.modules.worker.guard import WorkerLock
from catcher.modules.worker.handlers import Done, HandlerContext


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


def _enqueue_raw(engine, job_type: str) -> None:
    """Queue a job straight into the table: `jobs add` refuses a type with no handler."""
    with session_scope(engine) as session:
        enqueue(session, type=job_type, now=utc_now())


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
    assert result.output.splitlines() == [f"job queued, version {__version__}, job id {job.id}"]
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


COMMA_NAME = "youtube gemini summary - Unfortunately, YouTube Really Is This Simple.md"


def test_jobs_add_only_and_requeue_are_always_lists_and_a_repeat_appends(runner: CliRunner, engine) -> None:
    result = runner.invoke(
        app,
        [
            "jobs", "add", "pipeline.run",
            "--param", "only=x",
            "--param", f"only={COMMA_NAME}",
            "--param", "requeue=7",
            "--param", "requeue=true",
            "--param", "limit=2",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    [job] = _jobs(engine)
    assert job.params == {"only": ["x", COMMA_NAME], "requeue": ["7", "true"], "limit": 2}  # no comma split

    one = runner.invoke(app, ["jobs", "add", "pipeline.run", "--param", "only=YouTube walks"])
    assert one.exit_code == 0, one.output
    assert _jobs(engine)[-1].params == {"only": ["YouTube walks"]}


def test_jobs_add_still_refuses_a_repeated_plain_param(runner: CliRunner, engine) -> None:
    result = runner.invoke(app, ["jobs", "add", "pipeline.run", "--param", "limit=1", "--param", "limit=2"])
    assert result.exit_code == 2
    assert "given twice" in result.output
    assert _jobs(engine) == []


def test_jobs_add_refuses_an_unknown_job_type(runner: CliRunner, engine) -> None:
    result = runner.invoke(app, ["jobs", "add", "pipeline.rnu"])
    assert result.exit_code == 2
    assert "unknown job type 'pipeline.rnu'" in result.output
    assert "pipeline.run" in result.output  # the known types are listed
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert _jobs(engine) == []


@pytest.mark.parametrize(
    ("job_type", "params", "message"),
    [
        ("pipeline.run", ["limit=-1"], "limit"),
        ("pipeline.run", ["colour=blue"], "colour"),
        ("pipeline.run", ["only=../x"], "only"),
        ("pipeline.run", ["profile="], "profile"),
        ("pipeline.run", ["refresh_facts=maybe"], "refresh_facts"),
        ("youtube.fetch", ["calculated_name=clippings/x.md", "refresh_facts=7"], "refresh_facts"),
        ("llm.reason", [], "calculated_name"),
        ("llm.reason", ["calculated_name=../x.md"], "calculated_name"),
        ("llm.reason", ["calculated_name=notes/x.md", "refresh_llm=maybe"], "refresh_llm"),
        ("youtube.fetch", ["calculated_name=/etc/x.md"], "calculated_name"),
        ("pipeline.publish", ["push=maybe"], "push"),
        ("pipeline.publish", ["branch=main"], "branch"),
    ],
)
def test_jobs_add_checks_the_params_with_the_handlers_own_parser(
    runner: CliRunner, engine, job_type: str, params: list[str], message: str
) -> None:
    args = [arg for param in params for arg in ("--param", param)]
    result = runner.invoke(app, ["jobs", "add", job_type, *args])
    assert result.exit_code == 2, result.output
    assert f"cannot queue {job_type}" in result.output and message in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert _jobs(engine) == []


def test_jobs_add_accepts_good_params_for_every_handler(runner: CliRunner, engine) -> None:
    for args in (
        ["pipeline.run", "--param", "only=x", "--param", "retry_deferred=true"],
        ["llm.reason", "--param", "calculated_name=20261002-abcdef-x.md", "--param", "profile=fake"],
        ["youtube.fetch", "--param", "calculated_name=clippings/2026/20261002-abcdef-x.md"],
        ["pipeline.publish", "--param", "push=false", "--param", "pull=false"],
    ):
        result = runner.invoke(app, ["jobs", "add", *args])
        assert result.exit_code == 0, (args, result.output)
    assert len(_jobs(engine)) == 4


def test_refresh_facts_is_validated_by_jobs_add(runner: CliRunner, engine) -> None:
    bad = runner.invoke(app, ["jobs", "add", "pipeline.run", "--param", "refresh_facts=yes"])
    assert bad.exit_code == 2 and "refresh_facts must be true or false" in bad.output
    assert _jobs(engine) == []
    good = runner.invoke(app, ["jobs", "add", "pipeline.run", "--param", "refresh_facts=true"])
    assert good.exit_code == 0, good.output
    [job] = _jobs(engine)
    assert job.params == {"refresh_facts": True}


def test_jobs_list_filters_by_status(runner: CliRunner, engine) -> None:
    ids = []
    for job_type, params in (
        ("pipeline.run", []),
        ("youtube.fetch", ["--param", "calculated_name=clippings/x.md"]),
        ("llm.reason", ["--param", "calculated_name=notes/x.md"]),
    ):
        result = runner.invoke(app, ["jobs", "add", job_type, *params])
        assert result.exit_code == 0, result.output
        ids.append(result.output.strip().rsplit("job id ", 1)[1])
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


def _local_clock(until: datetime, now: datetime) -> str:
    """The gate's style: the local time, with the date when it is not today."""
    moment, today = until.astimezone(), now.astimezone().date()
    return moment.strftime("%H:%M") if moment.date() == today else moment.strftime("%Y-%m-%d %H:%M")


@pytest.mark.parametrize(
    ("column", "ahead"), [("next_allowed_at", timedelta(minutes=10)), ("blocked_until", timedelta(hours=20))]
)
def test_jobs_list_explains_a_fetch_job_that_waits_for_its_resource(
    runner: CliRunner, engine, monkeypatch: pytest.MonkeyPatch, column: str, ahead: timedelta
) -> None:
    now = utc_now()
    monkeypatch.setattr(cli, "utc_now", lambda: now)
    with session_scope(engine) as session:
        fetch, _ = enqueue(session, type="youtube.fetch", now=now, resource="youtube")
        other, _ = enqueue(session, type="llm.reason", now=now)
        fetch_id, other_id = fetch.id, other.id

    open_gate = runner.invoke(app, ["jobs", "list"])
    assert open_gate.exit_code == 0, open_gate.output
    assert "waiting for" not in open_gate.output

    with session_scope(engine) as session:
        session.execute(
            text(f"update resources set {column} = :until where name = 'youtube'"), {"until": now + ahead}
        )
    closed = runner.invoke(app, ["jobs", "list"])
    assert closed.exit_code == 0, closed.output
    lines = {line.split()[0]: line for line in closed.output.strip().splitlines()}
    assert lines[str(fetch_id)].split()[1:3] == ["youtube.fetch", "queued"]
    assert lines[str(fetch_id)].endswith(f"  waiting for youtube until {_local_clock(now + ahead, now)}")
    assert "waiting for" not in lines[str(other_id)]


def test_a_fetch_job_added_by_hand_waits_while_the_youtube_gate_is_closed(
    runner: CliRunner, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`jobs add youtube.fetch` carries the resource, as the pipeline's own fetch jobs do: the claim skips it
    while the gate is closed, so it is never claimed and deferred for nothing."""
    now = utc_now()
    monkeypatch.setattr(cli, "utc_now", lambda: now)
    result = runner.invoke(app, ["jobs", "add", "youtube.fetch", "--param", "calculated_name=clippings/x.md"])
    assert result.exit_code == 0, result.output
    other = runner.invoke(app, ["jobs", "add", "llm.reason", "--param", "calculated_name=notes/x.md"])
    assert other.exit_code == 0, other.output
    resources = {job.type: job.resource for job in _jobs(engine)}
    assert resources == {"youtube.fetch": "youtube", "llm.reason": None}

    with session_scope(engine) as session:
        session.execute(
            text("update resources set blocked_until = :until where name = 'youtube'"),
            {"until": now + timedelta(hours=6)},
        )
    with session_scope(engine) as session:
        claimed = queue.claim(session, worker="w", now=now, lease_s=60)
        assert claimed is not None and claimed.type == "llm.reason"
        assert queue.claim(session, worker="w", now=now, lease_s=60) is None  # the fetch job waits
    listed = runner.invoke(app, ["jobs", "list", "--status", "queued"])
    [line] = listed.output.strip().splitlines()
    assert line.split()[1:3] == ["youtube.fetch", "queued"]
    assert line.endswith(f"  waiting for youtube until {_local_clock(now + timedelta(hours=6), now)}")


def test_jobs_list_never_replaces_an_error_with_the_wait(
    runner: CliRunner, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = utc_now()
    monkeypatch.setattr(cli, "utc_now", lambda: now)
    with session_scope(engine) as session:
        fetch, _ = enqueue(session, type="youtube.fetch", now=now, resource="youtube")
        fetch.error = "the last attempt crashed"  # queued again after a failed attempt
        session.execute(
            text("update resources set blocked_until = :until where name = 'youtube'"),
            {"until": now + timedelta(hours=6)},
        )
    result = runner.invoke(app, ["jobs", "list"])
    [line] = result.output.strip().splitlines()
    assert line.endswith("  the last attempt crashed")


def test_jobs_list_shows_no_wait_for_a_damaged_resource_time(runner: CliRunner, engine) -> None:
    """'infinity' cannot be loaded into Python, and the claim treats it as open: no crash, no wait shown."""
    with session_scope(engine) as session:
        enqueue(session, type="youtube.fetch", now=utc_now(), resource="youtube")
        session.execute(text("update resources set blocked_until = 'infinity' where name = 'youtube'"))

    result = runner.invoke(app, ["jobs", "list"])

    assert result.exit_code == 0, result.output
    [line] = result.output.strip().splitlines()
    assert line.split()[1:3] == ["youtube.fetch", "queued"]
    assert "waiting for" not in line


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
    _enqueue_raw(engine, "test.unknown")

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
    _enqueue_raw(engine, "test.echo")
    with WorkerLock(engine):
        result = runner.invoke(app, ["worker", "--once"])
    assert result.exit_code == 2
    assert "another worker or run is already running; one at a time" in result.output
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


# A real `catcher worker` process: outside the test process, so outside the network guard and the autouse
# fixtures. It must not read the developer's .env (API keys, the real repos): `load_dotenv` is switched off in
# the process itself, and the repos and keys it could reach are set to throwaway values.
WORKER = [
    sys.executable,
    "-c",
    "import catcher.cli as cli; cli.load_dotenv = lambda *args, **kwargs: False; cli.app()",
    "worker",
]


def _worker_env(database_url: str, tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "CATCHER_ALLOW_NETWORK"}
    return {
        **env,
        "DATABASE_URL": database_url,  # the test database, never the developer's own
        "IDEAS_REPO": str(tmp_path / "idea-bucket"),
        "DOCS_REPO": str(tmp_path / "epiaku-docs"),
        "OPENAI_API_KEY": "",
        "FREELLMAPI_API_KEY": "",
        "FREELLMAPI_URL": "http://127.0.0.1:1/v1",
        "LOG_FILE": str(tmp_path / "worker.log"),
        "LOG_LEVEL": "INFO",
    }


def test_an_idle_worker_process_stops_at_once_on_sigterm(
    runner: CliRunner, fresh_database_url: str, tmp_path: Path
) -> None:
    env = _worker_env(fresh_database_url, tmp_path)
    command = [*WORKER, "--poll-s", "3600"]
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


def test_a_worker_that_loses_its_lock_stops_with_exit_1(
    runner: CliRunner, engine, fresh_database_url: str, tmp_path: Path
) -> None:
    env = _worker_env(fresh_database_url, tmp_path)
    command = [*WORKER, "--poll-s", "0.1"]
    process = subprocess.Popen(command, env=env, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stderr is not None
        for line in process.stderr:  # wait until it holds the lock and runs
            if "worker" in line and "started" in line:
                break
        else:
            pytest.fail(f"the worker never started (exit {process.wait(5)})")
        with engine.connect() as admin:  # a Postgres restart, or a dropped connection, ends the lock
            [pid] = admin.execute(
                text("select pid from pg_locks where locktype = 'advisory' and granted")
            ).scalars()
            admin.execute(text("select pg_terminate_backend(:pid)"), {"pid": pid})
            admin.commit()
        assert process.wait(timeout=10) == 1
        rest = process.stderr.read()
        assert "lost its database lock" in rest
        assert "Traceback" not in rest
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_worker_once_exits_1_when_a_claim_hits_a_database_error(
    runner: CliRunner, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enqueue_raw(engine, "test.echo")

    def broken_claim(session, **kwargs):
        raise OperationalError("select ... for update", {}, Exception("connection refused"))

    monkeypatch.setattr(queue, "claim", broken_claim)
    result = runner.invoke(app, ["worker", "--once"])

    assert result.exit_code == 1, result.output
    assert "ran 0 job(s)" in result.output
    assert "could not claim a job" in result.output
    [job] = _jobs(engine)
    assert job.status == "queued"


def test_worker_once_exits_1_when_a_job_could_not_be_finished(
    runner: CliRunner, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enqueue_raw(engine, "test.echo")

    def broken_run_job(*args, **kwargs):
        raise OperationalError("update jobs", {}, Exception("connection lost"))

    monkeypatch.setattr(loop, "run_job", broken_run_job)
    result = runner.invoke(app, ["worker", "--once"])

    assert result.exit_code == 1, result.output
    assert "error=1" in result.output
    assert "could not be finished" in result.output


def test_worker_once_with_a_failed_job_still_exits_0(
    runner: CliRunner, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enqueue_raw(engine, "test.unknown")  # no handler: the job fails, the worker did its work
    result = runner.invoke(app, ["worker", "--once"])
    assert result.exit_code == 0, result.output
    assert "failed=1" in result.output


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://catcher:s3cr3t-pw@localhost:notaport/catcher",
        "not a database url s3cr3t-pw",
        "nosuchdriver://catcher:s3cr3t-pw@localhost/catcher",
    ],
)
@pytest.mark.parametrize("command", [["worker", "--once"], ["jobs", "list"], ["jobs", "add", "pipeline.run"]])
def test_a_malformed_database_url_exits_2_with_a_short_message_and_no_password(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, url: str, command: list[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    result = runner.invoke(app, command)
    assert result.exit_code == 2, result.output
    assert "DATABASE_URL is not a valid database URL" in result.output
    assert "s3cr3t-pw" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_typer_never_shows_local_variables_in_a_traceback() -> None:
    assert app.pretty_exceptions_show_locals is False  # older Typer versions default to True
