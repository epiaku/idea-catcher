"""`catcher publish` over the worker path (real Postgres, real Git repos with local bare remotes).

The command takes the worker's lock, queues ONE `pipeline.publish` (never a `pipeline.run`), drains it and
prints what was committed and pushed per repo."""

from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, select, text
from typer.testing import CliRunner
from worker_harness import DOCS_SEED, NOTES

from catcher.cli import app
from catcher.core.db import session_scope, utc_now
from catcher.modules.queue.models import Job
from catcher.modules.queue.queue import enqueue
from catcher.modules.worker import handlers_pipeline, runner
from catcher.modules.worker.guard import WorkerLock

pytestmark = pytest.mark.db


@pytest.fixture
def db_url(pg_engine: Engine) -> str:
    return pg_engine.url.render_as_string(hide_password=False)


@pytest.fixture
def repos(make_repo) -> SimpleNamespace:
    ideas_bare, ideas = make_repo("idea-bucket", {"inbox/notes/seed.md": "seed\n"})
    docs_bare, docs = make_repo("epiaku-docs", DOCS_SEED)
    return SimpleNamespace(ideas=ideas, docs=docs, ideas_bare=ideas_bare, docs_bare=docs_bare)


@pytest.fixture
def use_services(db_url: str, monkeypatch: pytest.MonkeyPatch, make_services) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    services = make_services()
    monkeypatch.setattr("catcher.cli.default_services", lambda settings: services)


def _args(repos, *extra: str) -> list[str]:
    return ["publish", "--ideas", str(repos.ideas), "--docs", str(repos.docs), *extra]


def _dirty(repos) -> None:
    """One change in a managed folder of each repo."""
    (repos.ideas / "inbox" / "notes" / "new.md").write_text("new\n", encoding="utf-8")
    (repos.docs / NOTES / "2026-new.md").write_text("page\n", encoding="utf-8")


def _jobs(engine: Engine) -> list[Job]:
    with session_scope(engine) as session:
        return list(session.scalars(select(Job).order_by(Job.created_at, Job.id)))


def test_publish_commits_both_repos_and_does_not_push_by_default(repos, use_services, pg_engine, sh) -> None:
    _dirty(repos)
    ideas_remote = sh(repos.ideas_bare, "rev-parse", "main")
    docs_remote = sh(repos.docs_bare, "rev-parse", "main")
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 0, result.output
    assert "committed: ideas yes, docs yes" in result.output
    assert "pushed: no" in result.output
    for repo in (repos.ideas, repos.docs):
        assert sh(repo, "status", "--porcelain") == ""
    assert sh(repos.ideas_bare, "rev-parse", "main") == ideas_remote
    assert sh(repos.docs_bare, "rev-parse", "main") == docs_remote
    [job] = _jobs(pg_engine)
    assert (job.type, job.status) == ("pipeline.publish", "succeeded")
    assert job.params == {"push": False, "pull": False}
    with WorkerLock(pg_engine):  # the command let the lock go
        pass


def test_publish_push_pushes_to_the_remotes(repos, use_services, pg_engine, sh) -> None:
    _dirty(repos)
    result = CliRunner().invoke(app, _args(repos, "--push"))
    assert result.exit_code == 0, result.output
    assert "pushed: yes" in result.output
    assert sh(repos.ideas_bare, "rev-parse", "main") == sh(repos.ideas, "rev-parse", "main")
    assert sh(repos.docs_bare, "rev-parse", "main") == sh(repos.docs, "rev-parse", "main")
    [job] = _jobs(pg_engine)
    assert job.params == {"push": True, "pull": True}


def test_publish_with_nothing_to_commit_says_so_and_exits_0(repos, use_services, pg_engine) -> None:
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 0, result.output
    assert "nothing to commit" in result.output


def test_publish_refuses_while_a_worker_runs(repos, use_services, pg_engine, sh) -> None:
    _dirty(repos)
    before = sh(repos.ideas, "log", "--oneline")
    with WorkerLock(pg_engine):
        result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 2, result.output
    assert "nothing was done" in result.output
    assert _jobs(pg_engine) == []
    assert sh(repos.ideas, "log", "--oneline") == before
    assert "?? " in sh(repos.ideas, "status", "--porcelain")


@pytest.mark.parametrize("job_type", ["pipeline.run", "pipeline.publish"])
def test_publish_refuses_over_an_earlier_queued_run(repos, use_services, pg_engine, job_type: str) -> None:
    _dirty(repos)
    with session_scope(pg_engine) as session:
        enqueue(session, type=job_type, now=utc_now(), params={"push": True, "pull": True})
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 2, result.output
    assert "nothing was done" in " ".join(result.output.split())
    assert [(job.type, job.status) for job in _jobs(pg_engine)] == [(job_type, "queued")]


def test_publish_queues_no_pipeline_run(repos, use_services, pg_engine) -> None:
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 0, result.output
    assert [job.type for job in _jobs(pg_engine)] == ["pipeline.publish"]


def test_a_failed_push_says_what_is_committed_and_what_was_pushed(
    repos, use_services, pg_engine, sh, tmp_path
) -> None:
    _dirty(repos)
    sh(repos.ideas, "remote", "set-url", "--push", "origin", str(tmp_path / "missing.git"))
    result = CliRunner().invoke(app, _args(repos, "--push"))
    assert result.exit_code == 1, result.output
    assert "committed: ideas yes, docs yes" in result.output
    assert "pushed: docs; push FAILED for: ideas" in result.output
    assert "nothing to commit" not in result.output
    assert sh(repos.docs_bare, "rev-parse", "main") == sh(repos.docs, "rev-parse", "main")
    # again, with nothing new to commit: ideas is still ahead and still fails; the output must not say so
    again = CliRunner().invoke(app, _args(repos, "--push"))
    assert again.exit_code == 1, again.output
    assert "committed: ideas no, docs no" in again.output
    assert "push FAILED for: ideas" in again.output and "nothing to commit" not in again.output


def test_push_on_a_repo_without_a_remote_fails_before_any_git_change(
    repos, use_services, pg_engine, sh
) -> None:
    _dirty(repos)
    sh(repos.docs, "remote", "remove", "origin")
    before = (sh(repos.ideas, "log", "--oneline"), sh(repos.docs, "log", "--oneline"))
    result = CliRunner().invoke(app, _args(repos, "--push"))
    assert result.exit_code == 1, result.output
    assert "no git remote" in " ".join(result.output.split())
    assert (sh(repos.ideas, "log", "--oneline"), sh(repos.docs, "log", "--oneline")) == before
    assert "??" in sh(repos.ideas, "status", "--porcelain")


def test_a_lock_lost_after_the_publish_says_the_publish_happened(
    repos, use_services, pg_engine, monkeypatch: pytest.MonkeyPatch, sh
) -> None:
    _dirty(repos)
    real_commit = handlers_pipeline.commit_managed
    calls: list[object] = []

    def commit_then_lose_the_lock(repo, *args, **kwargs):
        committed = real_commit(repo, *args, **kwargs)
        calls.append(repo)
        if len(calls) == 2:
            with pg_engine.connect() as admin:
                pids = admin.execute(
                    text(
                        "select pid from pg_locks where locktype = 'advisory' and granted"
                        " and database = (select oid from pg_database where datname = current_database())"
                    )
                ).scalars()
                for pid in list(pids):
                    admin.execute(text("select pg_terminate_backend(:pid, 5000)"), {"pid": pid})
        return committed

    monkeypatch.setattr(handlers_pipeline, "commit_managed", commit_then_lose_the_lock)
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 1, result.output
    assert "committed: ideas yes, docs yes" in result.output
    assert "the publish itself is done" in result.output
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_ctrl_c_during_the_publish_leaves_the_job_queued_and_says_so(
    repos, use_services, pg_engine, monkeypatch: pytest.MonkeyPatch, sh
) -> None:
    _dirty(repos)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(handlers_pipeline, "commit_managed", interrupt)
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 1, result.output
    assert "run catcher worker --once to finish it" in result.output
    assert [(job.type, job.status) for job in _jobs(pg_engine)] == [("pipeline.publish", "queued")]
    assert "??" in sh(repos.ideas, "status", "--porcelain")


def test_ctrl_c_before_the_publish_is_queued_does_not_claim_a_queued_job(
    repos, use_services, pg_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dirty(repos)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.Worker, "reap_safely", interrupt)
    result = CliRunner().invoke(app, _args(repos))
    assert result.exit_code == 1, result.output
    assert "nothing was queued or left" in result.output
    assert "worker --once" not in result.output
    assert _jobs(pg_engine) == []
