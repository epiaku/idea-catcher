"""`catcher run pipeline` over the worker path (real Postgres, real Git repos, fake model and YouTube).

The command takes the worker's lock, queues `pipeline.run`, drains it with the `Worker`, queues
`pipeline.publish` (`pull` = `push`) and prints the report built from the database."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, select, text
from typer.testing import CliRunner
from worker_harness import DOCS_SEED, GEMINI_CHAT, NOTES, YT_CLIP, FrozenClock

from catcher.cli import RUN_LOST, app
from catcher.core.config import Settings
from catcher.core.db import session_scope
from catcher.modules.llm.backends.fake import FakeBackend
from catcher.modules.llm.service import BackendReply, UsageLimitReached
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.worker.guard import WorkerAlreadyRunning, WorkerLock
from catcher.modules.worker.runner import run_command
from catcher.modules.youtube.access import YoutubeAccess
from catcher.modules.youtube.gate_rules import clock_text
from catcher.modules.youtube.pg_gate import PostgresGate

pytestmark = pytest.mark.db

TWO_NOTES = {"inbox/notes/first idea.md": "A first idea\n", "inbox/notes/second idea.md": "A second idea\n"}
GAP_S = 600


@pytest.fixture
def db_url(pg_engine: Engine) -> str:
    return pg_engine.url.render_as_string(hide_password=False)


def _repos(make_repo, ideas_files: dict[str, str]) -> SimpleNamespace:
    ideas_bare, ideas = make_repo("idea-bucket", ideas_files)
    docs_bare, docs = make_repo("epiaku-docs", DOCS_SEED)
    return SimpleNamespace(ideas=ideas, docs=docs, ideas_bare=ideas_bare, docs_bare=docs_bare)


@pytest.fixture
def repos(make_repo) -> SimpleNamespace:
    return _repos(make_repo, TWO_NOTES)


@pytest.fixture
def use_services(db_url: str, monkeypatch: pytest.MonkeyPatch, make_services):
    """Point the command at the test database and give it fake services (no real model, no YouTube)."""
    monkeypatch.setenv("DATABASE_URL", db_url)

    def use(services) -> None:
        monkeypatch.setattr("catcher.cli.default_services", lambda settings: services)

    use(make_services())
    return use


def _args(repos, *extra: str) -> list[str]:
    return ["run", "pipeline", "--ideas", str(repos.ideas), "--docs", str(repos.docs), *extra]


def _jobs(engine: Engine, job_type: str | None = None) -> list[Job]:
    with session_scope(engine) as session:
        statement = select(Job).order_by(Job.created_at, Job.id)
        if job_type is not None:
            statement = statement.where(Job.type == job_type)
        return list(session.scalars(statement))


def _items(engine: Engine) -> list[JobItem]:
    with session_scope(engine) as session:
        return list(session.scalars(select(JobItem).order_by(JobItem.calculated_name)))


def _inbox(ideas: Path) -> list[str]:
    return sorted(p.name for p in (ideas / "inbox").rglob("*.md"))


def _pages(docs: Path, folder: str = NOTES) -> list[str]:
    return sorted(p.name for p in (docs / folder).glob("2026*.md"))


def _advisory_lock_pids(engine: Engine) -> list[int]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "select pid from pg_locks where locktype = 'advisory' and granted"
                    " and database = (select oid from pg_database where datname = current_database())"
                )
            ).scalars()
        )


class HookBackend(FakeBackend):
    """A fake model that runs `hook(call_number)` before each reply (1 for the first call)."""

    def __init__(self, hook, replies=None) -> None:
        super().__init__(replies)
        self.hook = hook

    def complete(self, prompt: str, *, model: str | None, task: str) -> BackendReply:
        self.hook(len(self.prompts) + 1)
        return super().complete(prompt, model=model, task=task)


def test_run_pipeline_publishes_the_inbox_and_commits_and_prints_the_summary(
    repos, use_services, pg_engine, sh
) -> None:
    remote_before = sh(repos.ideas_bare, "rev-parse", "main")
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 0, result.output
    assert [line.split()[0] for line in result.stdout.splitlines()] == ["published", "published", "summary:"]
    assert "summary: {'published': 2} committed={'docs': True, 'ideas': True} pushed=False" in result.output
    assert _inbox(repos.ideas) == [] and len(_pages(repos.docs)) == 2
    for repo in (repos.ideas, repos.docs):
        assert sh(repo, "status", "--porcelain") == ""
    assert "idea-catcher: process the inbox (pipeline.publish)" in sh(repos.ideas, "log", "--oneline", "-1")
    assert sh(repos.ideas_bare, "rev-parse", "main") == remote_before  # nothing pushed without --push
    assert [(job.type, job.status) for job in _jobs(pg_engine) if job.type.startswith("pipeline.")] == [
        ("pipeline.run", "succeeded"),
        ("pipeline.publish", "succeeded"),
    ]
    with WorkerLock(pg_engine):  # the command let the lock go
        pass


def test_run_pipeline_options_become_job_params(repos, use_services, pg_engine) -> None:
    args = _args(repos, "--limit", "1", "--file", "first idea", "--requeue", "nope")
    args += ["--retry-deferred", "--profile", "fake", "--refresh-llm", "--refresh-facts"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output  # `nope` is not in archive/
    assert 'no document named "nope" in archive/' in result.output
    [run] = _jobs(pg_engine, "pipeline.run")
    assert run.params == {
        "limit": 1,
        "only": ["first idea"],
        "requeue": ["nope"],
        "retry_deferred": True,
        "profile": "fake",
        "refresh_llm": True,
        "refresh_facts": True,
    }
    [publish] = _jobs(pg_engine, "pipeline.publish")
    assert publish.params == {"push": False, "pull": False}
    plain = CliRunner().invoke(app, _args(repos))
    assert plain.exit_code == 0, plain.output
    assert _jobs(pg_engine, "pipeline.run")[-1].params == {}  # no option: no param


def test_run_pipeline_exits_1_when_a_document_failed(repos, use_services, make_services) -> None:
    use_services(make_services(note_backend=FakeBackend(["nope", "still nope", "nope", "still nope"])))
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 1, result.output
    assert sum(line.startswith("failed") for line in result.output.splitlines()) == 2
    assert "summary: {'failed': 2} committed={'docs': False, 'ideas': True} pushed=False" in result.output


def test_run_pipeline_without_push_only_commits(repos, use_services, sh) -> None:
    for repo in (repos.ideas, repos.docs):
        sh(repo, "remote", "remove", "origin")  # no remote: a pull or a push would fail
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 0, result.output
    assert "committed={'docs': True, 'ideas': True} pushed=False" in result.output
    assert sh(repos.ideas, "status", "--porcelain") == "" and sh(repos.docs, "status", "--porcelain") == ""


def test_run_pipeline_with_push_pushes_to_the_remote(repos, use_services, sh) -> None:
    result = CliRunner().invoke(app, _args(repos, "--push"))
    assert result.exit_code == 0, result.output
    assert "committed={'docs': True, 'ideas': True} pushed=True" in result.output
    for repo, bare in ((repos.ideas, repos.ideas_bare), (repos.docs, repos.docs_bare)):
        assert sh(bare, "rev-parse", "main") == sh(repo, "rev-parse", "HEAD")


def test_a_failed_push_is_a_problem_line_and_exit_1(repos, use_services, sh) -> None:
    sh(repos.ideas, "remote", "remove", "origin")  # --push with no remote: the publish job fails
    result = CliRunner().invoke(app, _args(repos, "--push"))
    assert result.exit_code == 1, result.output
    [error] = [line for line in result.output.splitlines() if line.startswith("error")]
    assert "no git remote to push to" in error and "not (fully) committed or pushed" in error
    assert "pushed=False" in result.output


# --- YouTube: the clips that wait for the gate ------------------------------------------------------------


@pytest.fixture
def clip_run(make_repo, make_services, db_url, yt_facts, worker_engine):
    """A note and a clip; the YouTube gate (frozen clock) allows the next fetch in GAP_S seconds."""
    repos = _repos(
        make_repo, {"inbox/notes/first idea.md": "A first idea\n", "inbox/clippings/yt.md": YT_CLIP}
    )
    clock = FrozenClock()
    fetch_calls: list[str] = []
    sleeps: list[float] = []

    def fetch(video_id: str):
        fetch_calls.append(video_id)
        return yt_facts.model_copy(update={"video_id": video_id})

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    gate = PostgresGate(worker_engine, min_gap_s=GAP_S, jitter_s=0, block_hours=6, clock=clock.timestamp)
    services = make_services()
    services.youtube = YoutubeAccess(fetch, gate, clock=clock.timestamp, sleep=clock.advance, wait_max_s=0)
    with session_scope(worker_engine) as session:
        session.execute(
            text("update resources set next_allowed_at = :at where name = 'youtube'"),
            {"at": clock() + timedelta(seconds=GAP_S)},
        )
    settings = Settings(ideas_repo=repos.ideas, docs_repo=repos.docs, database_url=db_url)

    def run(wait_youtube_s: float | None):
        return run_command(
            settings,
            ideas=repos.ideas,
            docs=repos.docs,
            params={},
            push=False,
            wait_youtube_s=wait_youtube_s,
            services=services,
            clock=clock,
            sleep=sleep,
        )

    return SimpleNamespace(repos=repos, clock=clock, fetch_calls=fetch_calls, sleeps=sleeps, run=run)


def test_run_pipeline_leaves_the_clips_that_wait_for_youtube_queued_and_says_so(clip_run, pg_engine) -> None:
    outcome = clip_run.run(wait_youtube_s=None)
    assert outcome.exit_code == 0 and not outcome.interrupted
    assert clip_run.fetch_calls == [] and clip_run.sleeps == []
    statuses = {item.doc_class: item.status for item in outcome.report.items}
    assert statuses == {"note": "published", "youtube": "waiting"}
    assert outcome.left_queued == 1
    [fetch] = _jobs(pg_engine, "youtube.fetch")
    assert fetch.status == "queued"  # a later `catcher worker` finishes it
    until = clock_text((clip_run.clock() + timedelta(seconds=GAP_S)).timestamp(), clip_run.clock.timestamp())
    assert outcome.blocked_lines == [f"1 clip(s) wait for YouTube until {until}: run catcher worker"]
    assert outcome.committed == {"docs": True, "ideas": True}  # the note is published and committed


def test_wait_youtube_keeps_polling_until_the_fetch_is_due(clip_run) -> None:
    outcome = clip_run.run(wait_youtube_s=1800)
    assert clip_run.sleeps == [GAP_S]  # one wait, as long as the gap; the clock was frozen otherwise
    assert clip_run.fetch_calls == ["nGVZS_wUDGM"]
    assert {item.status for item in outcome.report.items} == {"published"}
    assert outcome.exit_code == 0 and outcome.left_queued == 0 and outcome.blocked_lines == []
    assert _pages(clip_run.repos.docs, "hugo/content/en/docs/idea-bucket/youtube")


def test_wait_youtube_does_not_wait_longer_than_its_limit(clip_run) -> None:
    outcome = clip_run.run(wait_youtube_s=GAP_S - 1)
    assert clip_run.sleeps == [] and clip_run.fetch_calls == []
    assert outcome.left_queued == 1 and outcome.blocked_lines[0].startswith("1 clip(s) wait for YouTube")


def test_a_later_run_reports_the_clip_an_earlier_run_left_waiting(clip_run) -> None:
    """B5b ruling (B72 report scope): the clip of the first run is still waiting, then the second run's drain
    fetches and publishes it; both runs' reports list it."""
    first = clip_run.run(wait_youtube_s=None)
    assert {item.doc_class: item.status for item in first.report.items} == {
        "note": "published",
        "youtube": "waiting",
    }
    still = clip_run.run(wait_youtube_s=None)  # the gate is still closed: listed as waiting, no fetch
    assert [(item.doc_class, item.status) for item in still.report.items] == [("youtube", "waiting")]
    clip_run.clock.advance(GAP_S)
    second = clip_run.run(wait_youtube_s=None)
    assert [(item.doc_class, item.status) for item in second.report.items] == [("youtube", "published")]
    assert clip_run.fetch_calls == ["nGVZS_wUDGM"] and second.left_queued == 0


def test_a_run_after_ctrl_c_reports_the_documents_it_finished(
    repos, use_services, make_services, pg_engine
) -> None:
    def interrupt_the_first(call: int) -> None:
        if call == 1:
            raise KeyboardInterrupt

    use_services(make_services(note_backend=HookBackend(interrupt_the_first)))
    assert CliRunner().invoke(app, _args(repos)).exit_code == 1
    use_services(make_services())
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 0, result.output
    # the earlier run's two documents, each listed once
    assert [line.split()[0] for line in result.stdout.splitlines()] == ["published", "published", "summary:"]
    assert "summary: {'published': 2}" in result.output


# --- the lock, Ctrl-C -----------------------------------------------------------------------------------


def test_a_worker_cannot_start_during_the_run(repos, use_services, make_services, pg_engine) -> None:
    refused: list[str] = []

    def try_a_worker(call: int) -> None:
        try:
            with WorkerLock(pg_engine):
                refused.append("TAKEN")
        except WorkerAlreadyRunning:
            refused.append("refused")

    use_services(make_services(note_backend=HookBackend(try_a_worker)))
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 0, result.output
    assert refused == ["refused", "refused"]  # during each job of the run
    with WorkerLock(pg_engine):  # and free again afterwards
        pass


def test_ctrl_c_puts_the_running_job_back_to_queued_and_does_not_publish(
    repos, use_services, make_services, pg_engine, monkeypatch: pytest.MonkeyPatch, sh
) -> None:
    def interrupt_the_first(call: int) -> None:
        if call == 1:
            raise KeyboardInterrupt

    use_services(make_services(note_backend=HookBackend(interrupt_the_first)))
    commits = sh(repos.ideas, "log", "--oneline")
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 1, result.output
    assert "interrupted: 2 job(s) left queued, run catcher worker --once to finish" in result.output
    assert "summary:" not in result.output
    reasons = _jobs(pg_engine, "llm.reason")
    assert [(job.status, job.attempts) for job in reasons] == [("queued", 0), ("queued", 0)]  # no attempt
    assert _jobs(pg_engine, "pipeline.publish") == []
    assert sh(repos.ideas, "log", "--oneline") == commits

    # a following `catcher worker --once` finishes both documents: none lost, none twice
    monkeypatch.setattr("catcher.modules.worker.app.default_services", lambda settings, **kw: make_services())
    worker = CliRunner().invoke(
        app, ["worker", "--once", "--ideas", str(repos.ideas), "--docs", str(repos.docs)]
    )
    assert worker.exit_code == 0, worker.output
    assert [item.status for item in _items(pg_engine)] == ["published", "published"]
    assert len(_pages(repos.docs)) == 2
    assert len(list((repos.ideas / "archive").rglob("*.md"))) == 2
    assert not (repos.ideas / "failed").exists() and _inbox(repos.ideas) == []


def test_the_lock_lost_mid_run_exits_1_and_commits_nothing(
    repos, use_services, make_services, pg_engine, sh
) -> None:
    def lose_the_lock(call: int) -> None:
        if call == 1:
            [pid] = _advisory_lock_pids(pg_engine)
            with pg_engine.connect() as admin:
                assert admin.execute(text("select pg_terminate_backend(:pid, 5000)"), {"pid": pid}).scalar()

    use_services(make_services(note_backend=HookBackend(lose_the_lock)))
    commits = (sh(repos.ideas, "log", "--oneline"), sh(repos.docs, "log", "--oneline"))
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 1, result.output
    assert " ".join(RUN_LOST.split()[:6]) in " ".join(result.output.split())
    assert "1 document(s) were finished and are NOT committed" in result.output
    assert sum(line.startswith("published") for line in result.output.splitlines()) == 1
    assert (sh(repos.ideas, "log", "--oneline"), sh(repos.docs, "log", "--oneline")) == commits
    assert _jobs(pg_engine, "pipeline.publish") == []
    # none lost: the second document waits with its job queued for the next worker
    statuses = sorted(item.status for item in _items(pg_engine))
    assert statuses == ["published", "waiting_llm"]
    assert [job.status for job in _jobs(pg_engine, "llm.reason")] == ["succeeded", "queued"]


# --- the printed report -----------------------------------------------------------------------------------


def test_blocked_backends_are_summarised_in_one_line_each(make_repo, use_services, pg_engine) -> None:
    repos = _repos(make_repo, {"inbox/clippings/systeme.md": GEMINI_CHAT})
    with session_scope(pg_engine) as session:
        session.execute(
            text(
                "insert into resources"
                " (name, blocked_until, blocked_at, reason, streak, concurrency, updated_at) values"
                " ('openai', now() + interval '6 hours', now(), 'budget reached (openai)', 0, 1, now())"
            )
        )
        until = session.scalar(text("select blocked_until from resources where name = 'openai'"))
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if "blocked until" in line]
    now = until - timedelta(hours=6)
    assert lines == [
        f"openai blocked until {clock_text(until.timestamp(), now.timestamp())} (budget reached (openai)): "
        "1 document(s) deferred"
    ]
    assert sum(line.startswith("deferred") for line in result.output.splitlines()) == 1


def test_the_printed_report_and_summary_format_is_unchanged(repos, use_services, pg_engine) -> None:
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 0, result.output
    items = _items(pg_engine)
    expected = [
        f"published      note            {item.doc_id:<24} {Path(item.docs_page or '').name}"
        for item in items
    ]
    expected.append("summary: {'published': 2} committed={'docs': True, 'ideas': True} pushed=False")
    assert result.stdout.splitlines() == expected  # the log lines go to stderr


# --- ported from tests/integration/git (B5b Task 3): the command now needs a real database ----------------
# They ran Docker-free with the `no_run_lock` stand-in; a real run queues jobs in Postgres now. A first run
# that used to call the old loop directly goes through the command too (one path).

NOTE_AND_CHAT = {
    "inbox/notes/YouTube walks.md": "Create YouTube content walking around\n",
    "inbox/clippings/systeme.md": GEMINI_CHAT,
}
REPO = Path(__file__).parents[3]
SAVED = "(saved reply)"


@pytest.fixture
def note_and_chat(make_repo) -> SimpleNamespace:
    return _repos(make_repo, NOTE_AND_CHAT)


def test_cli_run_pipeline(note_and_chat, db_url, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("PROFILES_FILE", str(REPO / "profiles.yaml"))
    result = CliRunner().invoke(app, [*_args(note_and_chat), "--profile", "fake"])
    assert result.exit_code == 0, result.output
    assert "published" in result.output
    assert "summary:" in result.output


def test_cli_file_option_warns_and_exits_with_1_when_nothing_matches(
    note_and_chat, db_url, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("PROFILES_FILE", str(REPO / "profiles.yaml"))
    args = [*_args(note_and_chat), "--profile", "fake"]
    result = CliRunner().invoke(app, [*args, "--file", "Nope.md", "-f", "systeme"])
    assert result.exit_code == 1
    assert 'not-found      no document named "Nope.md"' in result.output
    assert "published" in result.output


def test_requeue_flag_on_the_command_line(note_and_chat, use_services) -> None:
    assert CliRunner().invoke(app, _args(note_and_chat)).exit_code == 0  # was: the old loop, called directly
    ok = CliRunner().invoke(app, _args(note_and_chat, "--requeue", "YouTube walks"))
    assert ok.exit_code == 0, ok.output
    assert "requeued" in ok.output and "published" in ok.output
    missing = CliRunner().invoke(app, _args(note_and_chat, "--requeue", "Nope"))
    assert missing.exit_code == 1 and 'no document named "Nope" in archive/' in missing.output


def test_retry_deferred_flag_on_the_command_line(
    note_and_chat, use_services, make_services, pg_engine
) -> None:
    chats = FakeBackend([UsageLimitReached("limit", backend="openai")])
    use_services(make_services(chat_backend=chats))
    assert CliRunner().invoke(app, _args(note_and_chat)).exit_code == 0  # was: the old loop, called directly
    # the usage limit now blocks openai in Postgres for LLM_BLOCK_S (it outlived the run, unlike Stage A's
    # in-run block): it is over by the time the user retries
    with session_scope(pg_engine) as session:
        session.execute(text("delete from resources where name = 'openai'"))
    use_services(make_services())
    ok = CliRunner().invoke(app, _args(note_and_chat, "--retry-deferred"))
    assert ok.exit_code == 0, ok.output
    assert "requeued" in ok.output and "published" in ok.output


def test_the_command_line_marks_only_the_item_served_from_a_saved_reply(note_and_chat, use_services) -> None:
    assert CliRunner().invoke(app, _args(note_and_chat)).exit_code == 0  # was: the old loop, called directly
    runner = CliRunner()

    saved = runner.invoke(app, _args(note_and_chat, "--requeue", "systeme"))
    assert saved.exit_code == 0, saved.output
    [line] = [x for x in saved.output.splitlines() if x.startswith("published")]
    assert line.endswith(SAVED) and ".md (saved reply)" in line

    fresh = runner.invoke(app, _args(note_and_chat, "--requeue", "YouTube walks", "--refresh-llm"))
    assert fresh.exit_code == 0, fresh.output
    [line] = [x for x in fresh.output.splitlines() if x.startswith("published")]
    assert SAVED not in line
    assert not any(x.startswith("requeued") and SAVED in x for x in saved.output.splitlines())


# --- fix round 1: a publish that happened, an earlier run's jobs, a database without tables --------------


def test_a_lock_lost_after_the_publish_says_the_commit_happened(
    repos, use_services, pg_engine, monkeypatch: pytest.MonkeyPatch, sh
) -> None:
    from catcher.modules.worker import handlers_pipeline

    real_commit = handlers_pipeline.commit_managed
    calls: list[Path] = []

    def commit_then_lose_the_lock(repo, *args, **kwargs):
        committed = real_commit(repo, *args, **kwargs)
        calls.append(repo)
        if len(calls) == 2:  # both repos are committed; the publish job still finishes
            [pid] = _advisory_lock_pids(pg_engine)
            with pg_engine.connect() as admin:
                assert admin.execute(text("select pg_terminate_backend(:pid, 5000)"), {"pid": pid}).scalar()
        return committed

    monkeypatch.setattr(handlers_pipeline, "commit_managed", commit_then_lose_the_lock)
    result = CliRunner().invoke(app, _args(repos, "--push"))
    assert result.exit_code == 1, result.output
    [publish] = _jobs(pg_engine, "pipeline.publish")
    assert publish.status == "succeeded" and publish.result == {
        "committed": {"docs": True, "ideas": True},
        "pushed": True,
    }
    assert "summary: {'published': 2} committed={'docs': True, 'ideas': True} pushed=True" in result.output
    assert "lost its database lock after the publish" in result.output
    assert "NOT committed" not in result.output and "committed nothing" not in result.output
    assert sh(repos.ideas, "status", "--porcelain") == ""


def _queue_old(engine: Engine, job_type: str, params: dict) -> None:
    from catcher.core.db import utc_now
    from catcher.modules.queue.queue import enqueue

    with session_scope(engine) as session:
        enqueue(session, type=job_type, now=utc_now(), params=params)


EARLIER = (
    "1 job(s) from an earlier run are still queued (pipeline.run/pipeline.publish): finish them with "
    "catcher worker --once (see catcher jobs list), then run this again: nothing was done"
)


@pytest.mark.parametrize(
    ("job_type", "params"),
    [("pipeline.run", {"limit": 1}), ("pipeline.publish", {"push": True, "pull": True})],
)
def test_an_earlier_runs_queued_job_refuses_the_run_and_changes_nothing(
    repos, use_services, pg_engine, sh, job_type: str, params: dict
) -> None:
    _queue_old(pg_engine, job_type, params)
    before = (_inbox(repos.ideas), sh(repos.ideas, "log", "--oneline"), sh(repos.docs, "log", "--oneline"))
    result = CliRunner().invoke(app, _args(repos, "--file", "first idea"))
    assert result.exit_code == 2, result.output
    assert EARLIER in " ".join(result.output.split())
    assert "summary:" not in result.output
    assert [(job.type, job.status) for job in _jobs(pg_engine)] == [(job_type, "queued")]  # nothing ran
    assert (
        _inbox(repos.ideas),
        sh(repos.ideas, "log", "--oneline"),
        sh(repos.docs, "log", "--oneline"),
    ) == before
    assert _items(pg_engine) == []
    with WorkerLock(pg_engine):  # the lock was let go
        pass


def test_an_earlier_runs_document_jobs_do_not_stop_the_run(repos, use_services, pg_engine) -> None:
    _queue_old(pg_engine, "llm.reason", {"calculated_name": "notes/20261001-abc123-gone.md"})
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 0, result.output
    assert "summary: {'published': 2}" in result.output
    statuses = {job.params.get("calculated_name"): job.status for job in _jobs(pg_engine, "llm.reason")}
    assert statuses["notes/20261001-abc123-gone.md"] == "failed"  # it ran too (its item is gone)


def test_a_database_without_tables_says_to_upgrade_it(
    repos, fresh_database_url: str, monkeypatch: pytest.MonkeyPatch, make_services, sh
) -> None:
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    monkeypatch.setattr("catcher.cli.default_services", lambda settings: make_services())
    before = (_inbox(repos.ideas), sh(repos.ideas, "log", "--oneline"))
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 2, result.output
    assert "the database has no tables yet: run catcher db upgrade" in result.output
    assert "may already have been moved" not in result.output
    assert (_inbox(repos.ideas), sh(repos.ideas, "log", "--oneline")) == before
