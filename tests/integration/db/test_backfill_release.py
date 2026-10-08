"""Releasing backfill videos as clip notes into `inbox/clippings/` (real Postgres; no YouTube, no LLM)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.db import make_engine, session_scope
from catcher.modules.backfill import release as release_module
from catcher.modules.backfill import store
from catcher.modules.backfill.release import ReleaseResult, daily_allowance, note_path, release
from catcher.modules.queue.models import BackfillVideo

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
A, B, C, D, E = (f"{c * 10}1" for c in "abcde")


def _note(ideas: Path, vid: str) -> Path:
    return ideas / "inbox" / "clippings" / f"youtube source - {vid}.md"


def _add(session, *vids: str, at: datetime = NOW - timedelta(days=1)) -> None:
    for offset, vid in enumerate(vids):
        store.add_pending(session, {vid: ("docs", f"{vid}.md")}, at + timedelta(seconds=offset))


def _status(session, vid: str) -> str:
    row = session.get(BackfillVideo, vid)
    assert row is not None
    return row.status


def test_release_writes_a_clip_note_and_marks_the_row_released(session, tmp_path) -> None:
    ideas = tmp_path / "ideas"
    _add(session, A)

    result = release(session, ideas, 5, NOW)

    assert result == ReleaseResult(released=[A], repaired=[])
    assert note_path(ideas, A) == _note(ideas, A)
    assert _note(ideas, A).read_text(encoding="utf-8") == (
        "---\n"
        f"source: https://www.youtube.com/watch?v={A}\n"
        "tags: [clippings]\n"
        "backfill: true\n"
        "created: 2026-10-08\n"
        "---\n"
        f"![](https://www.youtube.com/watch?v={A})\n"
    )
    assert _status(session, A) == "released"
    assert session.get(BackfillVideo, A).released_at == NOW
    assert [p.name for p in (ideas / "inbox" / "clippings").iterdir()] == [_note(ideas, A).name]  # no temp


def test_release_honours_the_limit_and_the_oldest_first_order(session, tmp_path) -> None:
    ideas = tmp_path / "ideas"
    _add(session, C, at=NOW - timedelta(hours=1))
    _add(session, B, A, at=NOW - timedelta(hours=5))

    result = release(session, ideas, 2, NOW)

    assert result.released == [B, A]
    assert not _note(ideas, C).exists()
    assert _status(session, C) == "pending"


def test_the_limit_is_per_rolling_24_hours_so_a_second_run_releases_only_the_rest(session, tmp_path) -> None:
    ideas = tmp_path / "ideas"
    _add(session, A, B)
    assert daily_allowance(session, 3, NOW) == 3
    assert release(session, ideas, daily_allowance(session, 3, NOW), NOW).released == [A, B]

    _add(session, C, D, E, at=NOW)
    later = NOW + timedelta(hours=5)
    assert store.released_since(session, later - timedelta(hours=24)) == 2
    assert daily_allowance(session, 3, later) == 1
    assert release(session, ideas, daily_allowance(session, 3, later), later).released == [C]

    assert daily_allowance(session, 3, later + timedelta(hours=1)) == 0
    assert daily_allowance(session, 1, later) == 0  # never below 0
    assert release(session, ideas, 0, later).released == []
    assert store.counts(session) == {"pending": 2, "released": 3}


def test_after_24_hours_the_budget_comes_back(session, tmp_path) -> None:
    ideas = tmp_path / "ideas"
    _add(session, A, B, C)
    release(session, ideas, 2, NOW)
    assert daily_allowance(session, 2, NOW + timedelta(hours=23, minutes=59)) == 0

    tomorrow = NOW + timedelta(hours=24, seconds=1)
    assert daily_allowance(session, 2, tomorrow) == 2
    assert release(session, ideas, daily_allowance(session, 2, tomorrow), tomorrow).released == [C]


def test_release_twice_does_not_release_a_video_twice(session, tmp_path) -> None:
    ideas = tmp_path / "ideas"
    _add(session, A, B)
    assert release(session, ideas, 5, NOW).released == [A, B]
    _note(ideas, A).write_text("edited by hand\n", encoding="utf-8")

    assert release(session, ideas, 5, NOW + timedelta(days=2)) == ReleaseResult(released=[], repaired=[])
    assert _note(ideas, A).read_text(encoding="utf-8") == "edited by hand\n"
    assert session.get(BackfillVideo, A).released_at == NOW


def test_an_existing_note_is_never_overwritten_and_the_row_is_repaired(session, tmp_path) -> None:
    ideas = tmp_path / "ideas"
    _add(session, A, B, C)
    _note(ideas, A).parent.mkdir(parents=True)
    _note(ideas, A).write_text("a crash left this\n", encoding="utf-8")  # written, row not yet marked
    elsewhere = ideas / "archive" / "clippings" / "20260901-abcdef-some-clip.md"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text(f'---\nsource: "https://youtu.be/{B}?si=x"\n---\nclip\n', encoding="utf-8")

    result = release(session, ideas, 5, NOW)

    assert result == ReleaseResult(released=[C], repaired=[A, B])
    assert _note(ideas, A).read_text(encoding="utf-8") == "a crash left this\n"
    assert not _note(ideas, B).exists()  # the video is already a clip in the idea bucket
    assert [_status(session, v) for v in (A, B, C)] == ["released"] * 3


def test_a_write_failure_leaves_the_row_pending_and_the_others_released(
    session, tmp_path, monkeypatch
) -> None:
    ideas = tmp_path / "ideas"
    _add(session, A, B, C)
    real = release_module._write_new

    def failing(path: Path, text: str) -> None:
        if B in path.name:
            raise OSError("disk full")
        real(path, text)

    monkeypatch.setattr(release_module, "_write_new", failing)

    result = release(session, ideas, 5, NOW)

    assert result == ReleaseResult(released=[A, C], repaired=[])
    assert [_status(session, v) for v in (A, B, C)] == ["released", "pending", "released"]
    assert not _note(ideas, B).exists()
    assert sorted(p.name for p in _note(ideas, A).parent.iterdir()) == sorted(
        [_note(ideas, A).name, _note(ideas, C).name]
    )


def test_a_row_with_an_odd_video_id_is_never_written(session, tmp_path) -> None:
    ideas = tmp_path / "ideas"
    store.add_pending(session, {"../../evil1": ("channel", "x")}, NOW - timedelta(days=1))
    _add(session, A)

    assert release(session, ideas, 5, NOW).released == [A]
    assert _status(session, "../../evil1") == "pending"
    assert [p.name for p in _note(ideas, A).parent.iterdir()] == [_note(ideas, A).name]


# ---- the command ---------------------------------------------------------------------------------------


@pytest.fixture
def runner(fresh_database_url: str, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    monkeypatch.delenv("BACKFILL_DAILY_LIMIT", raising=False)
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


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    docs, ideas = tmp_path / "docs", tmp_path / "ideas"
    (ideas / "inbox").mkdir(parents=True)
    docs.mkdir()
    (docs / "links.md").write_text(
        "".join(f"see https://www.youtube.com/watch?v={vid}\n" for vid in (A, B, C)), encoding="utf-8"
    )
    return docs, ideas


def _import(runner: CliRunner, repos: tuple[Path, Path], *extra: str):
    docs, ideas = repos
    return runner.invoke(app, ["youtube", "import", "--docs", str(docs), "--ideas", str(ideas), *extra])


def _rows(engine) -> dict[str, str]:
    with session_scope(engine) as session:
        return {row.video_id: row.status for row in session.scalars(select(BackfillVideo))}


def _clips(ideas: Path) -> list[str]:
    folder = ideas / "inbox" / "clippings"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def test_import_limit_prints_the_release_line_and_dry_run_writes_nothing(runner, engine, repos) -> None:
    _, ideas = repos

    dry = _import(runner, repos, "--limit", "2", "--dry-run")

    assert dry.exit_code == 0, dry.output
    assert f"would release {A}" in dry.output and f"would release {B}" in dry.output
    assert f"would release {C}" not in dry.output
    assert "would release 2 video(s) into inbox/clippings/" in dry.output
    assert _rows(engine) == {}
    assert _clips(ideas) == []

    result = _import(runner, repos, "--limit", "2")

    assert result.exit_code == 0, result.output
    assert "released 2 video(s) into inbox/clippings/ (1 still pending)" in result.output
    assert _rows(engine) == {A: "released", B: "released", C: "pending"}
    assert _clips(ideas) == [_note(ideas, A).name, _note(ideas, B).name]

    again = _import(runner, repos, "--limit", "2")

    assert again.exit_code == 0, again.output
    assert "daily allowance already used" in again.output
    assert "released 0 video(s) into inbox/clippings/ (1 still pending)" in again.output
    assert _rows(engine)[C] == "pending"


def test_import_without_a_flag_only_scans_and_stores(runner, engine, repos) -> None:
    _, ideas = repos
    result = _import(runner, repos)
    assert result.exit_code == 0, result.output
    assert "released" not in result.output
    assert set(_rows(engine).values()) == {"pending"}
    assert _clips(ideas) == []


def test_release_without_limit_uses_BACKFILL_DAILY_LIMIT(runner, engine, repos, monkeypatch) -> None:
    _, ideas = repos
    monkeypatch.setenv("BACKFILL_DAILY_LIMIT", "1")

    result = _import(runner, repos, "--release")

    assert result.exit_code == 0, result.output
    assert "released 1 video(s) into inbox/clippings/ (2 still pending)" in result.output
    assert _clips(ideas) == [_note(ideas, A).name]
