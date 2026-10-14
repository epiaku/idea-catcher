"""The `backfill.docs` handler: scan the docs repo, store new links, optionally release (no live calls)."""

from pathlib import Path

import pytest
from sqlalchemy import select

from catcher.core.db import session_scope
from catcher.modules.queue.models import BackfillVideo, Job
from catcher.modules.worker.app import build_handlers, check_job
from catcher.modules.worker.handlers import Done, Fail, HandlerResult
from catcher.modules.worker.handlers_backfill import handle_backfill_docs, parse_docs_params

pytestmark = pytest.mark.db

A, B, C = (f"{c * 10}1" for c in "abc")


def _link(vid: str) -> str:
    return f"see https://www.youtube.com/watch?v={vid}\n"


@pytest.fixture
def docs_with_links(harness, sh) -> Path:
    """The docs repo holds exactly three links (A, B, C) and B already has a page; all pushed."""
    docs = harness.docs
    for path in docs.rglob("*.md"):
        if ".git" not in path.parts:
            path.unlink()
    (docs / "links.md").write_text(_link(A) + _link(B) + _link(C), encoding="utf-8")
    (docs / "page.md").write_text(f"---\nvideo_id: {B}\n---\nbody\n", encoding="utf-8")
    sh(docs, "add", "-A")
    sh(docs, "commit", "-m", "links")
    sh(docs, "push")
    return docs


def run_job(harness, **params) -> HandlerResult:
    job_id = harness.add_job("backfill.docs", **params)
    with session_scope(harness.ctx.engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return handle_backfill_docs(harness.ctx, job)


def rows(harness) -> dict[str, BackfillVideo]:
    with session_scope(harness.ctx.engine) as session:
        found = list(session.scalars(select(BackfillVideo)))
        session.expunge_all()
    return {r.video_id: r for r in found}


def clippings(harness) -> list[Path]:
    folder = harness.ideas / "inbox" / "clippings"
    return sorted(folder.glob("*.md")) if folder.is_dir() else []


@pytest.mark.parametrize(
    "params",
    [{"x": 1}, {"release": "5"}, {"release": True}, {"release": -1}, {"pull": "yes"}, {"dry_run": 1}],
)
def test_params_reject_unknown_and_wrong_types(params) -> None:
    with pytest.raises(ValueError):
        parse_docs_params(params)
    with pytest.raises(ValueError):
        check_job("backfill.docs", params)


def test_params_defaults() -> None:
    parsed = parse_docs_params({})
    assert (parsed.dry_run, parsed.pull, parsed.release) == (False, True, None)
    assert "backfill.docs" in build_handlers()


def test_dry_run_writes_no_row_no_file(harness, docs_with_links) -> None:
    before = clippings(harness)

    result = run_job(harness, dry_run=True)

    assert isinstance(result, Done)
    assert result.result["dry_run"] is True
    assert result.result["new"] == 2
    assert rows(harness) == {}
    assert clippings(harness) == before


def test_stores_new_links_and_skips_known(harness, docs_with_links) -> None:
    result = run_job(harness)

    assert isinstance(result, Done)
    assert (result.result["new"], result.result["known"], result.result["found"]) == (2, 1, 3)
    assert result.result["dry_run"] is False
    assert result.result["allowance"] is None
    assert result.result["released"] == []
    assert set(rows(harness)) == {A, C}


def test_release_writes_clip_notes_within_the_cap(harness, docs_with_links) -> None:
    result = run_job(harness, release=1)

    assert isinstance(result, Done)
    assert len(result.result["released"]) == 1
    assert result.result["still_pending"] == 1
    assert result.result["allowance"] == 1
    vid = result.result["released"][0]
    note = harness.ideas / "inbox" / "clippings" / f"youtube source - {vid}.md"
    assert "backfill: true" in note.read_text(encoding="utf-8")


def test_no_release_key_releases_nothing(harness, docs_with_links) -> None:
    before = clippings(harness)

    result = run_job(harness)

    assert isinstance(result, Done)
    assert result.result["still_pending"] == 2
    assert clippings(harness) == before


def test_pull_true_without_remote_still_scans(harness, docs_with_links, sh) -> None:
    sh(harness.docs, "remote", "remove", "origin")

    result = run_job(harness, pull=True)

    assert isinstance(result, Done)
    assert result.result["new"] == 2


def test_pull_failure_is_fail_with_gits_message(harness, docs_with_links, sh, tmp_path) -> None:
    sh(harness.docs, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))

    result = run_job(harness)

    assert isinstance(result, Fail)
    assert "Could not read from remote repository" in result.error
    assert rows(harness) == {}
