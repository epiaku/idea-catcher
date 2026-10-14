"""The `backfill.docs` handler: scan the docs repo, store new links, optionally release (no live calls)."""

from pathlib import Path

import pytest
from sqlalchemy import select

from catcher.core.db import session_scope
from catcher.modules.pipeline.doctypes import DESTINATIONS, destination_dir
from catcher.modules.queue.models import BackfillVideo, Job
from catcher.modules.worker import handlers_pipeline
from catcher.modules.worker.app import build_handlers, check_job
from catcher.modules.worker.handlers import Done, Fail, HandlerResult
from catcher.modules.worker.handlers_backfill import handle_backfill_docs, parse_docs_params, release_cap

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
    [{"x": 1}, {"release": "5"}, {"release": -1}, {"release": 1.5}, {"pull": "yes"}, {"dry_run": 1}],
)
def test_params_reject_unknown_and_wrong_types(params) -> None:
    with pytest.raises(ValueError):
        parse_docs_params(params)
    with pytest.raises(ValueError):
        check_job("backfill.docs", params)


@pytest.mark.parametrize("value", [True, False, 0, 5])
def test_params_accept_release_as_bool_or_whole_number(value) -> None:
    parsed = parse_docs_params({"release": value})
    assert parsed.release == value and type(parsed.release) is type(value)
    assert parse_docs_params({"release": None}).release is True


def test_params_defaults() -> None:
    parsed = parse_docs_params({})
    assert (parsed.dry_run, parsed.pull, parsed.release) == (False, True, True)
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


def test_default_releases_this_scans_pending_up_to_the_default_limit(harness, docs_with_links) -> None:
    (harness.docs / "more.md").write_text(_link("d" * 10 + "1"), encoding="utf-8")
    harness.ctx.settings = harness.ctx.settings.model_copy(update={"backfill_daily_limit": 2})

    result = run_job(harness, pull=False)

    assert isinstance(result, Done)
    assert (result.result["new"], len(result.result["released"])) == (3, 2)
    assert result.result["still_pending"] == 1
    assert result.result["allowance"] == 2


@pytest.mark.parametrize("value", [False, 0])
def test_release_false_adds_but_releases_nothing(harness, docs_with_links, value) -> None:
    before = clippings(harness)

    result = run_job(harness, release=value)

    assert isinstance(result, Done)
    assert result.result["released"] == [] and result.result["allowance"] == 0
    assert result.result["still_pending"] == 2
    assert set(rows(harness)) == {A, C}
    assert clippings(harness) == before


@pytest.mark.parametrize(("value", "expected"), [(True, 10), (False, 0), (0, 0), (5, 5)])
def test_release_cap(value, expected) -> None:
    assert release_cap(value, 10) == expected


def test_release_int_is_the_cap(harness, docs_with_links) -> None:
    result = run_job(harness, release=1)

    assert len(result.result["released"]) == 1  # type: ignore[union-attr]
    assert result.result["still_pending"] == 1  # type: ignore[union-attr]


def test_a_pending_video_from_an_earlier_call_that_the_scan_finds_again_is_released(
    harness, docs_with_links
) -> None:
    run_job(harness, release=False)

    result = run_job(harness)

    assert isinstance(result, Done)
    assert (result.result["new"], result.result["known"]) == (0, 3)
    assert sorted(result.result["released"]) == [A, C]


def test_pending_videos_the_scan_did_not_find_are_not_released(harness, docs_with_links) -> None:
    now = harness.clock()
    with session_scope(harness.ctx.engine) as session:
        session.add(BackfillVideo(video_id="z" * 10 + "1", source="docs", found_in=None, found_at=now))

    result = run_job(harness)

    assert isinstance(result, Done)
    assert sorted(result.result["released"]) == [A, C]
    assert rows(harness)["z" * 10 + "1"].status == "pending"
    assert result.result["still_pending"] == 1


def test_dry_run_releases_nothing_by_default(harness, docs_with_links) -> None:
    before = clippings(harness)

    result = run_job(harness, dry_run=True)

    assert isinstance(result, Done)
    assert result.result["released"] == [] and result.result["allowance"] is None
    assert clippings(harness) == before and rows(harness) == {}


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


RESULT_KEYS = {
    "files",
    "found",
    "known",
    "new",
    "pending",
    "unreadable",
    "dry_run",
    "released",
    "repaired",
    "still_pending",
    "allowance",
}


def test_the_result_has_exactly_these_keys(harness, docs_with_links) -> None:
    result = run_job(harness, release=1)

    assert isinstance(result, Done)
    assert set(result.result) == RESULT_KEYS


def test_release_zero_releases_nothing(harness, docs_with_links) -> None:
    before = clippings(harness)

    result = run_job(harness, release=0)

    assert isinstance(result, Done)
    assert result.result["released"] == []
    assert result.result["allowance"] == 0
    assert result.result["still_pending"] == 2
    assert clippings(harness) == before


def test_a_dry_run_skips_the_pull_even_with_the_default(harness, docs_with_links, monkeypatch) -> None:
    def no_pull(*args, **kwargs) -> None:
        raise AssertionError("a dry run must not pull")

    monkeypatch.setattr(handlers_pipeline, "pull", no_pull)

    result = run_job(harness, dry_run=True)

    assert isinstance(result, Done)


def _remote_edit(sh, harness, tmp_path, rel: str, text: str) -> None:
    other = tmp_path / "other-docs"
    if not other.exists():
        sh(tmp_path, "clone", str(harness.docs.with_name(f"{harness.docs.name}.git")), str(other))
    sh(other, "pull")
    (other / rel).write_text(text, encoding="utf-8")
    sh(other, "add", "-A")
    sh(other, "commit", "-m", "remote edit")
    sh(other, "push")


def test_the_pull_commits_the_workers_pages_first_so_a_remote_edit_leaves_no_stash(
    harness, docs_with_links, sh, tmp_path
) -> None:
    rel = f"{destination_dir(DESTINATIONS[0])}/page.md"
    page = harness.docs / rel
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("base\n", encoding="utf-8")
    sh(harness.docs, "add", "-A")
    sh(harness.docs, "commit", "-m", "base page")
    sh(harness.docs, "push")
    _remote_edit(sh, harness, tmp_path, rel, "theirs\n")
    page.write_text("ours, written by llm.reason\n", encoding="utf-8")  # uncommitted managed change

    result = run_job(harness)

    assert isinstance(result, Fail)  # a clean abort of the rebase, not a conflicted stash pop
    assert sh(harness.docs, "stash", "list") == ""
    assert sh(harness.docs, "status", "--porcelain") == ""
    assert page.read_text(encoding="utf-8") == "ours, written by llm.reason\n"
    assert "idea-catcher" in sh(harness.docs, "log", "-1", "--format=%s")
