"""The tests' helper `run_on_worker` (tests/support/run_on_worker.py): the command's path with the test's
services, and the report shape and statuses the old loop `run_pipeline` returned."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from run_on_worker import run_on_worker, run_outcome, run_params
from sqlalchemy import Engine, select
from worker_harness import DOCS_SEED, GEMINI_CHAT, NOTES, WEB_CLIPS

from catcher.cli import _run_params
from catcher.core.db import session_scope
from catcher.modules.llm.backends.fake import FakeBackend
from catcher.modules.pipeline.report import ItemReport, RunReport
from catcher.modules.queue.models import Job

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _database(pg_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))


@pytest.fixture
def repos(make_repo) -> SimpleNamespace:
    files = {"inbox/notes/YouTube walks.md": "A walk\n", "inbox/clippings/systeme.md": GEMINI_CHAT}
    ideas_bare, ideas = make_repo("idea-bucket", files)
    docs_bare, docs = make_repo("epiaku-docs", DOCS_SEED)
    return SimpleNamespace(ideas=ideas, docs=docs, ideas_bare=ideas_bare, docs_bare=docs_bare)


def _jobs(engine: Engine) -> list[tuple[str, str]]:
    with session_scope(engine) as session:
        return [(j.type, j.status) for j in session.scalars(select(Job).order_by(Job.created_at, Job.id))]


def test_a_run_returns_the_old_report_shape_and_commits(repos, make_services, pg_engine, sh) -> None:
    report = run_on_worker(repos, make_services())
    assert isinstance(report, RunReport)
    assert all(isinstance(item, ItemReport) for item in report.items)
    assert report.counts() == {"published": 2}
    assert sorted(item.doc_class for item in report.items) == ["ai-chat", "note"]
    assert {item.doc_id for item in report.items if item.doc_class == "ai-chat"} == {"cf81e40b020519ef"}
    pages = {item.page for item in report.items}
    on_disk = {p.name for folder in (NOTES, WEB_CLIPS) for p in (repos.docs / folder).glob("2026*.md")}
    assert pages == on_disk
    assert all(item.tokens_in is not None and not item.llm_saved for item in report.items)
    assert report.committed == {"docs": True, "ideas": True} and report.pushed is False
    assert report.unreadable == {} and report.not_found == [] and report.not_in_archive == []
    assert report.problems == []
    assert sh(repos.ideas, "status", "--porcelain") == "" and sh(repos.docs, "status", "--porcelain") == ""
    assert ("pipeline.run", "succeeded") in _jobs(pg_engine) and ("pipeline.publish", "succeeded") in _jobs(
        pg_engine
    )


def test_the_statuses_failed_skipped_and_not_found(repos, make_services) -> None:
    chats = FakeBackend(["nope", "still nope"])
    failed = run_on_worker(repos, make_services(chat_backend=chats), only=["systeme", "Nope"])
    assert failed.counts() == {"failed": 1} and "invalid output" in failed.items[0].message
    assert failed.not_found == ["Nope"]
    limited = run_on_worker(repos, make_services(), limit=0)
    assert limited.counts() == {"skipped": 1} and limited.items[0].message == "run limit reached"


def test_push_reaches_the_remotes(repos, make_services, sh) -> None:
    report = run_on_worker(repos, make_services(), push=True)
    assert report.pushed is True
    assert sh(repos.docs_bare, "rev-parse", "main") == sh(repos.docs, "rev-parse", "HEAD")


def test_a_dry_run_is_the_preview_and_touches_nothing(repos, make_services, pg_engine, sh) -> None:
    report = run_on_worker(repos, make_services(), dry_run=True)
    assert report.counts() == {"would_call_llm": 2}  # no saved reply in llm/ yet
    assert _jobs(pg_engine) == []
    assert sh(repos.ideas, "status", "--porcelain") == ""
    assert sorted(p.name for p in (repos.ideas / "inbox").rglob("*.md")) == ["YouTube walks.md", "systeme.md"]


def test_a_wrong_path_is_a_problem(repos, make_services, pg_engine) -> None:
    wrong = SimpleNamespace(ideas=repos.ideas / "nope", docs=repos.docs)
    outcome = run_outcome(wrong, make_services())
    assert outcome.exit_code == 2 and "not found at" in outcome.report.problems[0]
    assert _jobs(pg_engine) == []


def test_the_params_are_the_commands() -> None:
    options = {
        "profile": "fake",
        "limit": 1,
        "only": ["a"],
        "requeue": ["b"],
        "retry_deferred": True,
        "refresh_llm": True,
        "refresh_facts": True,
        "push": True,
        "wait_youtube": True,
        "dry_run": False,
    }
    expected = _run_params("fake", 1, ["a"], ["b"], True, True, True)
    assert run_params(options) == expected
    assert run_params({}) == _run_params(None, None, None, None, False, False, False) == {}
    with pytest.raises(TypeError, match="lock_check"):
        run_params({"lock_check": lambda: None})


def test_the_services_get_the_test_database(repos, make_services, pg_engine) -> None:
    services = make_services()
    run_on_worker(repos, services)
    assert services.settings.database_url == pg_engine.url.render_as_string(hide_password=False)
    assert Path(services.settings.ideas_repo) == repos.ideas
