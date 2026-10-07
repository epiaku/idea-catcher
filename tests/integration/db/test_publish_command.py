"""`catcher publish` over the worker path (real Postgres, real Git repos with local bare remotes).

The command takes the worker's lock, queues ONE `pipeline.publish` (never a `pipeline.run`), drains it and
prints what was committed and pushed per repo."""

from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, select
from typer.testing import CliRunner
from worker_harness import DOCS_SEED, NOTES

from catcher.cli import app
from catcher.core.db import session_scope, utc_now
from catcher.modules.queue.models import Job
from catcher.modules.queue.queue import enqueue
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
