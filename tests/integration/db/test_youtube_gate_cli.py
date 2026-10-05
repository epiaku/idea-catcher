"""`catcher youtube gate` (the Postgres gate as a person reads it) and `catcher youtube facts` through that
gate, on a migrated fresh database (real Postgres, CliRunner)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from catcher import cli
from catcher.cli import app
from catcher.core.db import make_engine
from catcher.modules.youtube.access import YoutubeAccess
from catcher.modules.youtube.facts import FactsDeferred, YoutubeFacts
from catcher.modules.youtube.gate_rules import clock_text
from catcher.modules.youtube.pg_gate import PostgresGate

pytestmark = pytest.mark.db

HOUR = 3600.0
NOW = datetime.now(UTC).replace(microsecond=0)  # a real moment (local times are printed), frozen per test
T = NOW.timestamp()


@pytest.fixture
def runner(fresh_database_url: str, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    monkeypatch.setenv("YOUTUBE_BLOCK_HOURS", "6")
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)
    runner = CliRunner()
    result = runner.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    return runner


@pytest.fixture
def engine(fresh_database_url: str, runner: CliRunner):
    db = make_engine(fresh_database_url)
    try:
        yield db
    finally:
        db.dispose()


def _row(engine: Engine) -> tuple:
    """The youtube row as epoch seconds (computed by Postgres), NULL as 0."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "select coalesce(extract(epoch from next_allowed_at)::float8, 0),"
                " coalesce(extract(epoch from blocked_until)::float8, 0),"
                " coalesce(extract(epoch from blocked_at)::float8, 0), streak"
                " from resources where name = 'youtube'"
            )
        ).one()
    return tuple(row)


def _set_row(engine: Engine, *, next_allowed_at=0.0, blocked_until=0.0, blocked_at=0.0, streak=0) -> None:
    def moment(seconds: float) -> datetime | None:
        return datetime.fromtimestamp(seconds, UTC) if seconds else None

    with engine.begin() as connection:
        connection.execute(
            text(
                "update resources set next_allowed_at = :n, blocked_until = :b, blocked_at = :a, streak = :s"
                " where name = 'youtube'"
            ),
            {"n": moment(next_allowed_at), "b": moment(blocked_until), "a": moment(blocked_at), "s": streak},
        )


def _clock(until: float) -> str:
    return clock_text(until, T)


# ---- catcher youtube gate --------------------------------------------------------------------------------


def test_gate_shows_an_open_gate(runner: CliRunner, engine) -> None:
    result = runner.invoke(app, ["youtube", "gate"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["youtube: open"]


def test_gate_shows_a_block_until_a_time(runner: CliRunner, engine) -> None:
    _set_row(engine, blocked_until=T + 20 * HOUR, blocked_at=T - HOUR, streak=2)
    result = runner.invoke(app, ["youtube", "gate"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [f"youtube: blocked until {_clock(T + 20 * HOUR)} (block 2)"]


def test_gate_shows_the_gap(runner: CliRunner, engine) -> None:
    _set_row(engine, next_allowed_at=T + 300)
    result = runner.invoke(app, ["youtube", "gate"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [f"youtube: next call allowed at {_clock(T + 300)}"]


class TooManyRequests(Exception):
    status = 429


def test_a_block_the_gate_could_not_record_is_shown_once_the_database_answers_again(
    runner: CliRunner, engine, fresh_database_url: str
) -> None:
    """A 429 while the database fails is held in memory; when the gate answers again the block is written
    into the row, so `catcher youtube gate` shows it and a restarted worker (a new access) keeps to it."""
    calls: list[str] = []

    def fetch(video_id: str) -> YoutubeFacts:
        calls.append(video_id)
        raise TooManyRequests("HTTP Error 429: Too Many Requests")

    gate = PostgresGate(engine, min_gap_s=0, jitter_s=0, block_hours=6, clock=lambda: T)
    access = YoutubeAccess(fetch, gate, clock=lambda: T, unrecorded_block_s=6 * HOUR)
    real_record_block = gate.record_block
    broken = make_engine(engine.url.set(host="127.0.0.1", port=1))  # nothing listens: connection refused

    def record_block_while_the_database_is_down(started_at: float | None = None) -> float:
        gate.engine = broken
        try:
            return real_record_block(started_at)
        finally:
            gate.engine = engine

    gate.record_block = record_block_while_the_database_is_down  # type: ignore[method-assign]
    try:
        with pytest.raises(FactsDeferred):
            access.get("AAAAAAAAAAA", facts_dir=None)
    finally:
        broken.dispose()
        gate.record_block = real_record_block  # type: ignore[method-assign]
    assert runner.invoke(app, ["youtube", "gate"]).output.splitlines() == ["youtube: open"]  # memory only

    with pytest.raises(FactsDeferred, match="blocked until"):  # the database answers again
        access.get("BBBBBBBBBBB", facts_dir=None)
    blocked = [f"youtube: blocked until {_clock(T + 6 * HOUR)} (block 1)"]
    assert runner.invoke(app, ["youtube", "gate"]).output.splitlines() == blocked
    assert _row(engine)[1:] == (T + 6 * HOUR, T, 1)

    restarted = YoutubeAccess(fetch, PostgresGate(engine, min_gap_s=0, jitter_s=0, clock=lambda: T + 60))
    with pytest.raises(FactsDeferred, match="blocked until"):
        restarted.get("CCCCCCCCCCC", facts_dir=None)
    assert calls == ["AAAAAAAAAAA"]  # one call, the one that got the 429


# ---- the database is unreachable or DATABASE_URL is malformed --------------------------------------------


def test_gate_exits_2_when_the_database_is_unreachable(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://catcher:s3cr3t-pw@127.0.0.1:1/catcher")
    result = runner.invoke(app, ["youtube", "gate"])
    assert result.exit_code == 2, result.output
    assert result.output.count("cannot reach the database in DATABASE_URL") == 1  # said once, not twice
    assert "s3cr3t-pw" not in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_gate_exits_2_on_a_malformed_database_url_without_the_password(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://catcher:s3cr3t-pw@localhost:notaport/catcher")
    result = runner.invoke(app, ["youtube", "gate"])
    assert result.exit_code == 2, result.output
    assert "DATABASE_URL is not a valid database URL" in result.output
    assert "s3cr3t-pw" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# ---- catcher youtube facts goes through the Postgres gate ------------------------------------------------


@pytest.fixture
def fetches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The real `build_access`, with YouTube itself replaced: the videos it was asked for."""
    from catcher.modules.youtube import access as access_mod

    calls: list[str] = []

    def fetch_facts(video_id: str, **kwargs) -> YoutubeFacts:
        calls.append(video_id)
        return YoutubeFacts(video_id=video_id, url="u", fetched_at="2026-10-04")

    monkeypatch.setattr(access_mod, "fetch_facts", fetch_facts)
    return calls


def test_youtube_facts_exits_2_when_the_gate_is_unavailable(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, fetches: list[str]
) -> None:
    url = "postgresql+psycopg://catcher:s3cr3t-pw@127.0.0.1:1/catcher"
    monkeypatch.setenv("DATABASE_URL", url)
    result = runner.invoke(app, ["youtube", "facts", "https://youtu.be/MBPHU7aaklM"])
    assert result.exit_code == 2, result.output
    assert "the YouTube gate is unavailable" in result.output
    assert "cannot reach the database in DATABASE_URL" in result.output
    assert url not in result.output and "s3cr3t-pw" not in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert fetches == []  # no gate, no call to YouTube


def test_youtube_facts_goes_through_the_postgres_gate(
    runner: CliRunner, engine, monkeypatch: pytest.MonkeyPatch, fetches: list[str]
) -> None:
    monkeypatch.setattr(cli, "build_access", _access_on_the_frozen_clock)
    _set_row(engine, blocked_until=T + 2 * HOUR, blocked_at=T - HOUR, streak=1)
    blocked = runner.invoke(app, ["youtube", "facts", "MBPHU7aaklM"])
    assert blocked.exit_code == 2, blocked.output
    assert "blocked until" in blocked.output
    assert fetches == []  # the block in the row stops it: YouTube is not asked

    _set_row(engine)  # the gate is open again
    monkeypatch.setenv("YOUTUBE_MIN_GAP_S", "600")
    monkeypatch.setenv("YOUTUBE_GAP_JITTER_S", "0")
    ok = runner.invoke(app, ["youtube", "facts", "MBPHU7aaklM"])
    assert ok.exit_code == 0, ok.output
    assert '"video_id": "MBPHU7aaklM"' in ok.output
    assert fetches == ["MBPHU7aaklM"]
    assert _row(engine)[0] == T + 600  # the fetch reserved the gap in the row


def _access_on_the_frozen_clock(settings):
    from catcher.modules.youtube.access import build_access

    return build_access(settings, clock=lambda: T)
