"""`catcher youtube import` and `catcher youtube backlog` (scan and store; real Postgres, CliRunner)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.db import make_engine, session_scope
from catcher.modules.backfill import store
from catcher.modules.queue.models import BackfillVideo, JobItem
from catcher.modules.youtube.pg_gate import PostgresGate

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
A, B, C, D = (f"{c}{c}{c}{c}{c}{c}{c}{c}{c}{c}1" for c in "abcd")


def _link(vid: str) -> str:
    return f"see https://www.youtube.com/watch?v={vid}\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    docs, ideas = tmp_path / "docs", tmp_path / "ideas"
    docs.mkdir()
    ideas.mkdir()
    return docs, ideas


def _import(runner: CliRunner, repos: tuple[Path, Path], *extra: str):
    docs, ideas = repos
    return runner.invoke(app, ["youtube", "import", "--docs", str(docs), "--ideas", str(ideas), *extra])


def _rows(engine) -> dict[str, BackfillVideo]:
    with session_scope(engine) as session:
        rows = list(session.scalars(select(BackfillVideo)))
        session.expunge_all()
    return {r.video_id: r for r in rows}


def test_import_stores_only_videos_without_a_page(runner, engine, repos) -> None:
    docs, _ = repos
    _write(docs / "a.md", _link(A) + _link(B))
    _write(docs / "page.md", f"---\nvideo_id: {B}\n---\nbody\n")

    result = _import(runner, repos)

    assert result.exit_code == 0, result.output
    line = "scanned 2 file(s), found 2 video(s), 1 already have a page, "
    line += "1 new in the backlog (1 pending in all)"
    assert line in result.output
    rows = _rows(engine)
    assert set(rows) == {A}
    assert rows[A].source == "docs"
    assert rows[A].found_in == "a.md"
    assert rows[A].status == "pending"


def test_import_twice_adds_nothing_the_second_time(runner, engine, repos) -> None:
    docs, _ = repos
    _write(docs / "a.md", _link(A))
    assert _import(runner, repos).exit_code == 0

    result = _import(runner, repos)

    assert result.exit_code == 0, result.output
    assert "1 already have a page, 0 new in the backlog (1 pending in all)" in result.output
    assert set(_rows(engine)) == {A}


def test_dry_run_writes_nothing_and_prints_the_counts(runner, engine, repos) -> None:
    docs, _ = repos
    _write(docs / "a.md", _link(A) + _link(C))
    _write(docs / "bad.md", "---\nx: [unclosed\n---\n")

    result = _import(runner, repos, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "found 2 video(s), 0 already have a page, 2 new in the backlog (2 pending in all)" in result.output
    assert _rows(engine) == {}


def test_import_skips_videos_already_in_job_items_or_the_idea_bucket(runner, engine, repos) -> None:
    docs, ideas = repos
    _write(docs / "a.md", _link(A) + _link(B) + _link(C) + _link(D))
    _write(ideas / "inbox/clippings/x.md", f'---\nsource: "https://youtu.be/{B}"\n---\n')
    with session_scope(engine) as session:
        session.add(
            JobItem(
                calculated_name=f"youtube/{C}.md",
                doc_id=C,
                doc_class="youtube",
                status="published",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    result = _import(runner, repos)

    assert result.exit_code == 0, result.output
    assert "found 4 video(s), 2 already have a page, 2 new" in result.output
    assert set(_rows(engine)) == {A, D}


def test_import_reports_unreadable_files(runner, engine, repos, monkeypatch) -> None:
    docs, _ = repos
    _write(docs / "a.md", _link(A))
    real = Path.read_text

    def broken(self, *args, **kwargs):
        if self.name == "bad.md":
            raise OSError("denied")
        return real(self, *args, **kwargs)

    _write(docs / "bad.md", "x")
    monkeypatch.setattr(Path, "read_text", broken)

    result = _import(runner, repos)

    assert result.exit_code == 0, result.output
    assert "skipped 1 unreadable file(s)" in result.output


def test_import_exit_2_for_a_missing_docs_folder_or_an_unreachable_database(
    runner, repos, tmp_path, monkeypatch
) -> None:
    docs, ideas = repos
    nope = str(tmp_path / "nope")
    missing = runner.invoke(app, ["youtube", "import", "--docs", nope, "--ideas", str(ideas)])
    assert missing.exit_code == 2, missing.output
    no_ideas = runner.invoke(app, ["youtube", "import", "--docs", str(docs), "--ideas", nope])
    assert no_ideas.exit_code == 2, no_ideas.output

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://catcher:s3cr3t-pw@127.0.0.1:1/catcher")
    result = _import(runner, repos)
    assert result.exit_code == 2, result.output
    assert "s3cr3t-pw" not in result.output
    assert "Traceback" not in result.output

    monkeypatch.setenv("DATABASE_URL", "not a url")
    assert _import(runner, repos).exit_code == 2


def test_import_exit_2_when_the_database_is_not_upgraded(repos, monkeypatch, fresh_database_url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    result = _import(CliRunner(), repos)
    assert result.exit_code == 2, result.output
    assert "catcher db upgrade" in result.output


def test_backlog_lists_counts_and_the_oldest_pending(runner, engine) -> None:
    with session_scope(engine) as session:
        store.add_pending(session, {A: ("docs", "a.md")}, NOW.replace(hour=1))
        store.add_pending(session, {B: ("docs", "b.md")}, NOW.replace(hour=2))
        store.add_pending(session, {C: ("docs", "c.md")}, NOW.replace(hour=3))
        store.mark_released(session, A, NOW)

    result = runner.invoke(app, ["youtube", "backlog", "--limit", "1"])

    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert lines[0] == "pending 2, released 1"
    assert lines[1:] == [f"{B}  b.md"]
    assert set(_rows(engine)) == {A, B, C}


# ---- --channel: the paced listing (a fake extractor; yt-dlp is never called) ----------------------------

CHANNEL = "https://www.youtube.com/@small"


class FakeListing:
    def __init__(self, ids: list[str] | None = None, error: Exception | None = None) -> None:
        self.ids = ids or []
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def __call__(self, url: str, *, max_videos: int) -> dict:
        self.calls.append((url, max_videos))
        if self.error is not None:
            raise self.error
        return {"entries": [{"id": v, "url": f"https://www.youtube.com/watch?v={v}"} for v in self.ids]}


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch):
    """The CLI's extractor, replaced: `build_extractor` would wrap yt-dlp."""
    monkeypatch.setenv("YOUTUBE_OFFLINE", "0")
    monkeypatch.setenv("YOUTUBE_MIN_GAP_S", "100")
    monkeypatch.setenv("YOUTUBE_GAP_JITTER_S", "0")
    fake = FakeListing()
    built: list[float] = []

    def build(request_delay_s: float):
        built.append(request_delay_s)
        return fake

    monkeypatch.setattr("catcher.modules.backfill.channel.build_extractor", build)
    fake.built = built  # type: ignore[attr-defined]
    return fake


def _gate(engine) -> PostgresGate:
    return PostgresGate(engine, block_hours=6)


def test_import_channel_takes_one_gate_slot_and_stores_the_ids(runner, engine, repos, listing) -> None:
    docs, _ = repos
    _write(docs / "page.md", f"---\nvideo_id: {B}\n---\nbody\n")
    listing.ids = [A, B, C]
    before = _gate(engine).snapshot()

    result = _import(runner, repos, "--channel", CHANNEL, "--max-videos", "7")

    assert result.exit_code == 0, result.output
    assert listing.calls == [(f"{CHANNEL}/videos", 7)]
    assert listing.built == [10.0]
    after = _gate(engine).snapshot()
    assert after.next_allowed_at > before.next_allowed_at  # one slot, the gap to the next call started
    assert after.blocked_until == 0
    assert f"listed 3 video(s) from {CHANNEL}: 1 already known, 2 new in the backlog" in result.output
    rows = _rows(engine)
    assert set(rows) == {A, C}
    assert {(r.source, r.found_in, r.status) for r in rows.values()} == {("channel", CHANNEL, "pending")}


def test_import_channel_with_a_closed_gate_calls_nothing_and_stores_nothing(
    runner, engine, repos, listing
) -> None:
    docs, _ = repos
    _write(docs / "a.md", _link(D))
    listing.ids = [A]
    closed = _gate(engine).close(hours=6)

    result = _import(runner, repos, "--channel", CHANNEL)

    assert result.exit_code == 1, result.output
    assert "YouTube blocked until" in result.output
    assert "nothing was listed" in result.output
    assert listing.calls == []
    assert _rows(engine) == {}
    assert _gate(engine).snapshot() == closed  # no slot taken


def test_import_channel_offline_or_a_bad_url_calls_nothing(
    runner, engine, repos, listing, monkeypatch
) -> None:
    listing.ids = [A]
    bad = _import(runner, repos, "--channel", f"https://www.youtube.com/watch?v={A}")
    assert bad.exit_code == 2, bad.output
    assert "not a YouTube channel" in bad.output

    monkeypatch.setenv("YOUTUBE_OFFLINE", "1")
    offline = _import(runner, repos, "--channel", CHANNEL)
    assert offline.exit_code == 1, offline.output
    assert "YOUTUBE_OFFLINE" in offline.output

    assert listing.calls == []
    assert _rows(engine) == {}
    assert _gate(engine).snapshot().next_allowed_at == 0


def test_a_429_opens_the_breaker_and_exits_1(runner, engine, repos, listing) -> None:
    listing.error = RuntimeError("ERROR: [youtube:tab] @small: HTTP Error 429: Too Many Requests")

    result = _import(runner, repos, "--channel", CHANNEL, "--channel", "https://www.youtube.com/@other")

    assert result.exit_code == 1, result.output
    assert "YouTube blocked until" in result.output
    assert len(listing.calls) == 1  # the second channel is not tried
    state = _gate(engine).snapshot()
    assert state.blocked_until > 0 and state.streak == 1
    assert _rows(engine) == {}


def test_an_ordinary_listing_error_exits_1_without_opening_the_breaker(
    runner, engine, repos, listing
) -> None:
    listing.error = RuntimeError("ERROR: [youtube:tab] @small: This channel does not exist.")

    result = _import(runner, repos, "--channel", CHANNEL)

    assert result.exit_code == 1, result.output
    assert "could not list" in result.output and "does not exist" in result.output
    assert _gate(engine).snapshot().blocked_until == 0
    assert _rows(engine) == {}


def test_dry_run_with_a_channel_takes_no_gate_slot(runner, engine, repos, listing) -> None:
    listing.ids = [A]

    result = _import(runner, repos, "--dry-run", "--channel", CHANNEL, "--channel", "https://youtube.com/@b")

    assert result.exit_code == 0, result.output
    assert "would list 2 channel(s), up to 200 video(s) each" in result.output
    assert listing.calls == [] and listing.built == []
    assert _gate(engine).snapshot().next_allowed_at == 0
    assert _rows(engine) == {}


def test_import_channel_with_an_unreachable_gate_calls_nothing(runner, repos, listing, monkeypatch) -> None:
    listing.ids = [A]
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://catcher:s3cr3t-pw@127.0.0.1:1/catcher")

    result = _import(runner, repos, "--channel", CHANNEL)

    assert result.exit_code == 2, result.output
    assert "nothing was listed" in result.output
    assert "s3cr3t-pw" not in result.output
    assert listing.calls == []
