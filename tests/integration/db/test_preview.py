"""`catcher run pipeline --dry-run` is a read-only preview of its own (B52-B55).

It reads the inbox, the saved replies in `llm/` and the saved facts in `facts/`, and says what a run would do.
It writes no file, no row and no job, leaves the YouTube gate alone, and calls neither a model nor YouTube."""

import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, func, select, text
from typer.testing import CliRunner
from worker_harness import DOCS_SEED, GEMINI_CHAT, YT_CLIP

from catcher.cli import app
from catcher.core.db import session_scope
from catcher.modules.pipeline.preview import preview
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.youtube.access import YoutubeAccess
from catcher.modules.youtube.pg_gate import PostgresGate

pytestmark = pytest.mark.db

VID = "nGVZS_wUDGM"
NOTE = "Create YouTube content walking around\n"


@pytest.fixture
def db_url(pg_engine: Engine) -> str:
    return pg_engine.url.render_as_string(hide_password=False)


@pytest.fixture
def repos(make_repo) -> SimpleNamespace:
    _, ideas = make_repo("idea-bucket", {"inbox/notes/walks.md": NOTE, "inbox/clippings/yt.md": YT_CLIP})
    _, docs = make_repo("epiaku-docs", DOCS_SEED)
    return SimpleNamespace(ideas=ideas, docs=docs)


def _explode(*args, **kwargs):
    raise AssertionError("the preview must not call the model or YouTube")


@pytest.fixture
def services(make_services, worker_engine):
    """Fake services: the model and the YouTube fetcher fail the test when called; the gate is real."""
    svc = make_services()
    svc.backends = _explode
    svc.facts = _explode
    gate = PostgresGate(worker_engine, min_gap_s=600, jitter_s=0, block_hours=6)
    svc.youtube = YoutubeAccess(_explode, gate)
    return svc


def _hash(*roots: Path) -> dict[str, str]:
    """Every file of the repos (the .git folders too) by content."""
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for root in roots
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _rows(engine: Engine) -> dict[str, object]:
    with session_scope(engine) as session:
        return {
            "items": session.scalar(select(func.count()).select_from(JobItem)),
            "jobs": session.scalar(select(func.count()).select_from(Job)),
            "resources": sorted(session.execute(text("select * from resources")).all()),
        }


def _by_id(report) -> dict[str, str]:
    return {item.doc_id: item.status for item in report.items}


def _save_facts(ideas: Path, yt_facts) -> None:
    (ideas / "facts").mkdir()
    (ideas / "facts" / f"{VID}.json").write_text(yt_facts.model_dump_json(indent=2), encoding="utf-8")


def test_the_preview_changes_nothing(repos, services, pg_engine) -> None:
    (repos.ideas / "inbox/notes/one.md").write_text("A\n", encoding="utf-8")
    (repos.ideas / "inbox/clippings/chat.md").write_text(GEMINI_CHAT, encoding="utf-8")
    (repos.ideas / "inbox/clippings/pic.png").write_bytes(b"\x89PNG")
    (repos.ideas / "inbox/notes/broken.md").write_text("---\n: [bad\n---\nx\n", encoding="utf-8")
    files, rows = _hash(repos.ideas, repos.docs), _rows(pg_engine)
    assert rows["items"] == 0 and rows["jobs"] == 0

    report = preview(repos.ideas, repos.docs, {"refresh_facts": True, "refresh_llm": True}, services)

    assert report.items and report.unreadable  # it did look at everything
    assert _hash(repos.ideas, repos.docs) == files  # no file written, moved or deleted, no .git change
    assert _rows(pg_engine) == rows  # no job, no item, the youtube row as it was (the gate is untouched)


def test_the_preview_says_what_a_run_would_do_per_document(repos, services) -> None:
    inbox = repos.ideas / "inbox"
    (inbox / "notes/short.md").write_text("A conversation\n", encoding="utf-8")
    (inbox / "notes/short again.md").write_text("A conversation\n\nand more\n", encoding="utf-8")
    (inbox / "clippings/pic.png").write_bytes(b"\x89PNG")
    (inbox / "notes/broken.md").write_text("---\n: [bad\n---\nx\n", encoding="utf-8")

    report = preview(repos.ideas, repos.docs, {}, services)

    by_class = {(i.doc_class, i.status) for i in report.items}
    assert ("note", "would_call_llm") in by_class  # no saved reply: a run would ask the model
    assert ("youtube", "would_fetch") in by_class  # no saved facts: a run would ask YouTube
    assert ("artifact", "would_copy") in by_class
    assert list(report.unreadable) == ["inbox/notes/broken.md"]
    assert "would_publish" not in {i.status for i in report.items}
    youtube = next(i for i in report.items if i.doc_class == "youtube")
    assert "no saved facts" in youtube.message
    assert report.committed == {} and report.pushed is False


def test_the_preview_reports_the_limit_the_names_and_the_duplicates(repos, services) -> None:
    (repos.ideas / "inbox/notes/b.md").write_text("Another idea\n", encoding="utf-8")
    report = preview(repos.ideas, repos.docs, {"limit": 1, "only": ["walks", "nothing like it"]}, services)
    assert report.not_found == ["nothing like it"]
    assert [(i.doc_class, i.status) for i in report.items] == [("note", "would_call_llm")]
    limited = preview(repos.ideas, repos.docs, {"limit": 1}, services)
    assert limited.counts().get("skipped") == 2 and len(limited.items) == 3  # one worked on, two left
    assert any(i.message == "run limit reached" for i in limited.items)


def test_the_preview_uses_saved_replies_and_saved_facts(
    repos, services, make_services, db_url, yt_facts, monkeypatch, sh
) -> None:
    _save_facts(repos.ideas, yt_facts)
    real = make_services()
    gate_free = YoutubeAccess(_explode, SimpleNamespace(peek=lambda: None))
    real.youtube = gate_free
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setattr("catcher.cli.default_services", lambda settings: real)
    args = ["--ideas", str(repos.ideas), "--docs", str(repos.docs)]
    done = CliRunner().invoke(app, ["db", "upgrade"])
    assert done.exit_code == 0, done.output
    first = CliRunner().invoke(app, ["run", "pipeline", *args])
    assert first.exit_code == 0, first.output
    assert "published" in first.output
    # the same texts captured again under other names: they match the saved replies and the saved facts
    (repos.ideas / "inbox/notes/walks again.md").write_text(NOTE, encoding="utf-8")
    (repos.ideas / "inbox/clippings/yt again.md").write_text(YT_CLIP, encoding="utf-8")
    files = _hash(repos.ideas, repos.docs)

    report = preview(repos.ideas, repos.docs, {}, services)  # its model and fetcher fail the test

    assert sorted(i.status for i in report.items) == ["would_publish", "would_publish"]
    for item in report.items:
        assert item.llm_saved and item.page and item.tokens_in is not None
    assert _hash(repos.ideas, repos.docs) == files  # no trace written, none marked, no facts saved

    asked = preview(
        repos.ideas, repos.docs, {"refresh_llm": True}, services
    )  # a fresh answer is a model call
    assert {i.status for i in asked.items} == {"would_call_llm"}
    refetch = preview(repos.ideas, repos.docs, {"refresh_facts": True}, services)
    assert {i.doc_class: i.status for i in refetch.items} == {
        "note": "would_publish",
        "youtube": "would_fetch",
    }
    again = preview(repos.ideas, repos.docs, {"requeue": ["walks"]}, services)  # archived by the first run
    assert [i.status for i in again.items] == ["would_requeue"]  # a dry run moves nothing back to inbox/
    assert _hash(repos.ideas, repos.docs) == files


def test_the_preview_does_not_need_the_docs_folder_and_logs_the_version(repos, services, caplog) -> None:
    caplog.set_level(logging.INFO, logger="catcher")
    report = preview(repos.ideas, repos.docs / "nope", {}, services)
    assert report.problems == [] and report.items
    started = [r.getMessage() for r in caplog.records if r.getMessage().startswith("run started")]
    assert started and "version=" in started[0] and "dry_run=True" in started[0]


@pytest.fixture
def runner(db_url, monkeypatch, services) -> CliRunner:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setattr("catcher.cli.default_services", lambda settings: services)
    cli = CliRunner()
    assert cli.invoke(app, ["db", "upgrade"]).exit_code == 0
    return cli


def _args(repos, *extra: str) -> list[str]:
    return ["run", "pipeline", "--ideas", str(repos.ideas), "--docs", str(repos.docs), *extra]


def test_the_preview_exit_codes_and_output_format_match_today(repos, runner, sh) -> None:
    result = runner.invoke(app, _args(repos, "--dry-run"))
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any(line.startswith(f"{'would_call_llm':<14} {'note':<15}") for line in lines)
    assert any(line.startswith(f"{'would_fetch':<14} {'youtube':<15}") for line in lines)
    assert lines[-1] == "summary: {'would_fetch': 1, 'would_call_llm': 1} committed={} pushed=False"
    assert sh(repos.ideas, "status", "--porcelain") == ""

    missing = runner.invoke(app, _args(repos, "--dry-run", "--file", "nothing like it"))
    assert missing.exit_code == 1  # a name that matched nothing
    assert 'not-found      no document named "nothing like it" in inbox/' in missing.output

    (repos.ideas / "inbox/notes/broken.md").write_text("---\n: [bad\n---\nx\n", encoding="utf-8")
    unreadable = runner.invoke(app, _args(repos, "--dry-run"))
    assert unreadable.exit_code == 1
    assert any(
        line.startswith("unreadable     inbox/notes/broken.md: ") for line in unreadable.output.splitlines()
    )

    wrong = runner.invoke(
        app, ["run", "pipeline", "--ideas", str(repos.ideas / "nope"), "--docs", str(repos.docs), "--dry-run"]
    )
    assert wrong.exit_code == 2  # a wrong path
    assert "error          idea-bucket inbox/ not found at" in wrong.output
