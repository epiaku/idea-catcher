"""`catcher youtube gate` (the Postgres gate as a person reads it) and `--import-file` (the Stage A state file
copied into the row once), on a migrated fresh database (real Postgres, CliRunner)."""

import json
from datetime import UTC, datetime
from pathlib import Path

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
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def runner(fresh_database_url: str, monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> CliRunner:
    monkeypatch.setenv("DATABASE_URL", fresh_database_url)
    monkeypatch.setenv("CATCHER_STATE_DIR", str(state_dir))  # never the real YouTube gate
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


def _write_state(state_dir: Path, **values: float) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {"next_allowed_at": 0.0, "blocked_until": 0.0, "blocked_at": 0.0, "streak": 0.0} | values
    path = state_dir / "youtube-gate.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


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


# ---- catcher youtube gate --import-file ------------------------------------------------------------------


def test_import_file_copies_a_block_into_the_row(runner: CliRunner, engine, state_dir: Path) -> None:
    path = _write_state(
        state_dir, next_allowed_at=T + 100, blocked_until=T + 12 * HOUR, blocked_at=T - 60, streak=2.0
    )
    result = runner.invoke(app, ["youtube", "gate", "--import-file"])
    assert result.exit_code == 0, result.output
    assert f"imported: blocked until {_clock(T + 12 * HOUR)} (block 2)" in result.output.splitlines()
    assert _row(engine) == (T + 100, T + 12 * HOUR, T - 60, 2)
    assert path.exists()  # copied, not moved

    shown = runner.invoke(app, ["youtube", "gate"])
    assert shown.output.splitlines() == [f"youtube: blocked until {_clock(T + 12 * HOUR)} (block 2)"]


def test_import_file_with_an_open_state_says_there_is_nothing_to_carry_over(
    runner: CliRunner, engine, state_dir: Path
) -> None:
    _write_state(state_dir, blocked_until=T - HOUR, blocked_at=T - 7 * HOUR, streak=0.0)  # an old block
    result = runner.invoke(app, ["youtube", "gate", "--import-file"])
    assert result.exit_code == 0, result.output
    assert "imported: open (nothing to carry over)" in result.output.splitlines()
    assert _row(engine) == (0.0, T - HOUR, T - 7 * HOUR, 0)


def test_import_file_never_shortens_a_longer_block(runner: CliRunner, engine, state_dir: Path) -> None:
    _set_row(engine, blocked_until=T + 20 * HOUR, blocked_at=T - HOUR, streak=3)
    before = _row(engine)
    _write_state(state_dir, blocked_until=T + 2 * HOUR, blocked_at=T - 2 * HOUR, streak=1.0)

    result = runner.invoke(app, ["youtube", "gate", "--import-file"])

    assert result.exit_code == 0, result.output  # a re-run is not a failure
    assert "nothing to import: the database already holds this state or a stricter one" in result.output
    assert _row(engine) == before

    # A shorter file block recorded later only moves `blocked_at` forward: the block and the streak stay.
    _write_state(state_dir, blocked_until=T + 2 * HOUR, blocked_at=T - 60, streak=1.0)
    newer = runner.invoke(app, ["youtube", "gate", "--import-file"])
    assert newer.exit_code == 0, newer.output
    assert f"imported: blocked until {_clock(T + 20 * HOUR)} (block 3)" in newer.output.splitlines()
    assert _row(engine) == (0.0, T + 20 * HOUR, T - 60, 3)


def test_import_file_without_a_file_says_so_and_changes_nothing(
    runner: CliRunner, engine, state_dir: Path
) -> None:
    before = _row(engine)
    result = runner.invoke(app, ["youtube", "gate", "--import-file"])
    assert result.exit_code == 2, result.output
    assert f"no state file at {state_dir / 'youtube-gate.json'}: nothing to import" in result.output
    assert _row(engine) == before
    assert not (state_dir / "youtube-gate.json").exists()


def test_a_damaged_state_file_imports_as_closed(runner: CliRunner, engine, state_dir: Path) -> None:
    state_dir.mkdir(parents=True)
    (state_dir / "youtube-gate.json").write_text("{not json", encoding="utf-8")

    result = runner.invoke(app, ["youtube", "gate", "--import-file"])

    assert result.exit_code == 0, result.output
    assert "damaged" in result.output and "youtube-gate.corrupt" in result.output
    assert f"imported: blocked until {_clock(T + 6 * HOUR)} (block 1)" in result.output.splitlines()
    assert (state_dir / "youtube-gate.corrupt").read_text(encoding="utf-8") == "{not json"  # kept for a look
    assert _row(engine) == (0.0, T + 6 * HOUR, T, 1)


# ---- the database is unreachable or DATABASE_URL is malformed --------------------------------------------


@pytest.mark.parametrize("command", [["youtube", "gate"], ["youtube", "gate", "--import-file"]])
def test_gate_exits_2_when_the_database_is_unreachable(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, state_dir: Path, command: list[str]
) -> None:
    _write_state(state_dir, blocked_until=T + HOUR, blocked_at=T, streak=1.0)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://catcher:s3cr3t-pw@127.0.0.1:1/catcher")
    result = runner.invoke(app, command)
    assert result.exit_code == 2, result.output
    assert result.output.count("cannot reach the database in DATABASE_URL") == 1  # said once, not twice
    assert "s3cr3t-pw" not in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("command", [["youtube", "gate"], ["youtube", "gate", "--import-file"]])
def test_gate_exits_2_on_a_malformed_database_url_without_the_password(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, state_dir: Path, command: list[str]
) -> None:
    _write_state(state_dir, blocked_until=T + HOUR, blocked_at=T, streak=1.0)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://catcher:s3cr3t-pw@localhost:notaport/catcher")
    result = runner.invoke(app, command)
    assert result.exit_code == 2, result.output
    assert "DATABASE_URL is not a valid database URL" in result.output
    assert "s3cr3t-pw" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_import_file_twice_says_the_second_time_that_nothing_changed(
    runner: CliRunner, engine, state_dir: Path
) -> None:
    _write_state(state_dir, blocked_until=T + 12 * HOUR, blocked_at=T - 60, streak=2.0)
    assert runner.invoke(app, ["youtube", "gate", "--import-file"]).exit_code == 0
    before = _row(engine)
    again = runner.invoke(app, ["youtube", "gate", "--import-file"])
    assert again.exit_code == 0, again.output  # a re-run by the user does not look like a failure
    assert "nothing to import: the database already holds this state or a stricter one" in again.output
    assert _row(engine) == before


def test_import_file_exits_2_when_a_damaged_file_cannot_be_repaired(
    runner: CliRunner, engine, state_dir: Path
) -> None:
    state_dir.mkdir(parents=True)
    (state_dir / "youtube-gate.lock").touch()  # the lock can be taken; the folder is read-only
    (state_dir / "youtube-gate.json").write_text("{not json", encoding="utf-8")
    before = _row(engine)
    state_dir.chmod(0o500)
    try:
        result = runner.invoke(app, ["youtube", "gate", "--import-file"])
    finally:
        state_dir.chmod(0o700)
    assert result.exit_code == 2, result.output
    assert "could not be read or repaired" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert _row(engine) == before
