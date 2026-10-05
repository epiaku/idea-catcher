"""The frontmatter mirror of an item's state: `stage`, `stage_reason` and `stage_since` of the working copy.

Postgres holds the truth; the mirror only shows it. It rewrites those three keys and nothing else, never
touches a finished page and never overwrites a file it cannot read."""

from datetime import UTC, datetime
from pathlib import Path

from catcher.core.frontmatter import load
from catcher.modules.pipeline.mirror import MirrorState, read_mirror, write_mirror

SINCE = datetime(2026, 10, 5, 9, 30, 0, tzinfo=UTC)

WORKING_COPY = (
    "---\n"
    "source: https://example.com/a\n"
    "created: 2026-09-25\n"
    "tags:\n"
    "- clippings\n"
    "original_filename: an idea.md\n"
    "calculated_filename: 2026-09-25-an-idea.md\n"
    "analyzed_at: '2026-10-05T09:00:00+00:00'\n"
    "stage: analyzed\n"
    "---\n"
    "The body stays.\n\n---\n\nEven with a rule in it.\n"
)


def put(tmp_path: Path, text: str, name: str = "an-idea.md") -> Path:
    path = tmp_path / "output" / "notes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_write_mirror_adds_the_three_keys_and_keeps_everything_else(tmp_path: Path) -> None:
    path = put(tmp_path, WORKING_COPY)
    before = load(path)

    assert write_mirror(path, stage="deferred", reason="the model is blocked", since=SINCE) is True

    after = load(path)
    assert after.body == before.body
    assert list(after.fm) == [*before.fm, "stage_reason", "stage_since"]  # `stage` keeps its place
    assert {k: v for k, v in after.fm.items() if k not in ("stage", "stage_reason", "stage_since")} == {
        k: v for k, v in before.fm.items() if k != "stage"
    }
    assert after.fm["stage"] == "deferred"
    assert after.fm["stage_reason"] == "the model is blocked"
    assert after.fm["stage_since"] == "2026-10-05T09:30:00+00:00"
    assert read_mirror(path) == MirrorState("deferred", "the model is blocked", SINCE)
    assert not list(path.parent.glob(".*.tmp"))  # the atomic write left no temp file


def test_write_mirror_removes_the_reason_when_there_is_none(tmp_path: Path) -> None:
    path = put(tmp_path, WORKING_COPY)
    write_mirror(path, stage="deferred", reason="the model is blocked", since=SINCE)

    later = datetime(2026, 10, 5, 10, 0, 0, tzinfo=UTC)
    assert write_mirror(path, stage="waiting_llm", reason=None, since=later) is True

    fm = load(path).fm
    assert "stage_reason" not in fm
    assert (fm["stage"], fm["stage_since"]) == ("waiting_llm", "2026-10-05T10:00:00+00:00")
    assert read_mirror(path) == MirrorState("waiting_llm", None, later)


def test_write_mirror_of_a_missing_file_returns_false(tmp_path: Path) -> None:
    path = tmp_path / "output" / "notes" / "gone.md"

    assert write_mirror(path, stage="waiting_llm", reason=None, since=SINCE) is False

    assert not path.exists() and not path.parent.exists()
    assert read_mirror(path) is None


def test_read_mirror_understands_the_stage_a_names(tmp_path: Path) -> None:
    analyzed = put(tmp_path, WORKING_COPY, "analyzed.md")
    deferred = put(
        tmp_path,
        "---\nid: x\nstage: deferred\ndeferred_at: '2026-10-04T08:00:00+02:00'\n"
        "deferred_reason: the backend is down\n---\nbody\n",
        "deferred.md",
    )

    assert read_mirror(analyzed) == MirrorState(
        "analyzed", None, datetime.fromisoformat("2026-10-05T09:00:00+00:00")
    )
    assert read_mirror(deferred) == MirrorState(
        "deferred", "the backend is down", datetime.fromisoformat("2026-10-04T08:00:00+02:00")
    )
    # the worker adds the new keys on top of the Stage A ones: the new keys win
    write_mirror(deferred, stage="deferred", reason="blocked until noon", since=SINCE)
    fm = load(deferred).fm
    assert fm["deferred_reason"] == "the backend is down"  # the Stage A keys are left as they were
    assert read_mirror(deferred) == MirrorState("deferred", "blocked until noon", SINCE)


def test_a_damaged_frontmatter_is_not_overwritten_and_reports_false(tmp_path: Path) -> None:
    damaged = put(tmp_path, "---\nstage: [analyzed\n---\nbody\n", "damaged.md")
    unclosed = put(tmp_path, "---\nstage: analyzed\nbody without a closing line\n", "unclosed.md")
    bare = put(tmp_path, "no frontmatter at all\n", "bare.md")
    before = {p: p.read_bytes() for p in (damaged, unclosed, bare)}

    for path in before:
        assert write_mirror(path, stage="waiting_llm", reason=None, since=SINCE) is False

    assert {p: p.read_bytes() for p in before} == before
    assert read_mirror(damaged) is None and read_mirror(unclosed) is None


def test_published_is_never_mirrored_into_a_page(tmp_path: Path) -> None:
    page = put(
        tmp_path,
        "---\ntitle: A page\nid: x\nstage: published\ncreated_by: idea-catcher\n---\nThe page.\n",
        "page.md",
    )
    working = put(tmp_path, WORKING_COPY)
    page_bytes, working_bytes = page.read_bytes(), working.read_bytes()

    # the finished page is never rewritten, whatever the target status
    assert write_mirror(page, stage="deferred", reason="late", since=SINCE) is False
    assert write_mirror(page, stage="published", reason=None, since=SINCE) is False
    # and `published` is never written into a working copy (the finished page replaces it)
    assert write_mirror(working, stage="published", reason=None, since=SINCE) is False

    assert page.read_bytes() == page_bytes and working.read_bytes() == working_bytes
    assert "stage_since" not in load(page).fm and "stage_reason" not in load(page).fm
