"""The YouTube gate in Postgres: the same behaviour as the file gate (a contract suite run on both), plus
what only a shared database brings (two workers at once, a missing or damaged row, time zones, errors)."""

import logging
import math
import threading
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, create_engine, event, text

from catcher.core.db import make_engine
from catcher.modules.youtube.gate import Gate, Wait, YoutubeGate
from catcher.modules.youtube.pg_gate import GateUnavailable, PostgresGate

pytestmark = pytest.mark.db

HOUR = 3600.0
GAP = 120.0
JITTER = 300.0
START = datetime(2026, 10, 4, 12, 0, tzinfo=UTC).timestamp()


class EpochClock:
    """Seconds since the epoch, frozen; it moves only when a test sets `now`."""

    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


MakeGate = Callable[..., Gate]


@pytest.fixture(params=["file", "postgres"])
def make_gate(request, tmp_path, pg_engine: Engine) -> MakeGate:
    """A factory for the gate under test; every gate it makes shares one state (a folder or the row)."""

    def make(clock: EpochClock, *, rng: Callable[[], float] = lambda: 0.5, hours: float = 6.0) -> Gate:
        options = {"min_gap_s": GAP, "jitter_s": JITTER, "block_hours": hours, "clock": clock, "rng": rng}
        if request.param == "file":
            return YoutubeGate(tmp_path / "state", **options)
        return PostgresGate(pg_engine, **options)

    return make


def _pg_gate(engine: Engine, clock: EpochClock, **options) -> PostgresGate:
    values = {"min_gap_s": GAP, "jitter_s": JITTER, "block_hours": 6.0, "rng": lambda: 0.5} | options
    return PostgresGate(engine, clock=clock, **values)


def _row(engine: Engine) -> tuple | None:
    """The youtube row as epoch seconds (computed by Postgres, not by the gate), or None when missing."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "select extract(epoch from next_allowed_at)::float8,"
                " extract(epoch from blocked_until)::float8, extract(epoch from blocked_at)::float8, streak,"
                " extract(epoch from updated_at)::float8"
                " from resources where name = 'youtube'"
            )
        ).first()
    return None if row is None else tuple(row)


def _execute(engine: Engine, sql: str, **params) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


# ---- the contract: the file gate and the Postgres gate behave the same ----------------------------------


def test_peek_changes_nothing(make_gate):
    clock = EpochClock()
    gate = make_gate(clock)
    for _ in range(3):
        assert gate.peek() is None
    assert gate.reserve() is None  # still the first call
    assert gate.peek() == Wait(START + GAP + 0.5 * JITTER, blocked=False)
    assert gate.peek() == Wait(START + GAP + 0.5 * JITTER, blocked=False)


def test_reserve_then_a_second_reserve_waits_for_the_gap(make_gate):
    clock = EpochClock()
    gate, other = make_gate(clock), make_gate(clock)  # two gates on one state: two runs or two workers
    assert gate.reserve() is None
    until = START + GAP + 0.5 * JITTER
    assert other.reserve() == Wait(until, blocked=False)
    clock.now = until - 1
    assert gate.reserve() == Wait(until, blocked=False)
    clock.now = until  # the gap ends at its moment
    assert other.reserve() is None
    assert gate.peek() == Wait(until + GAP + 0.5 * JITTER, blocked=False)  # and the next gap starts


def test_the_gap_has_jitter(make_gate):
    clock = EpochClock()
    most = make_gate(clock, rng=lambda: 1.0)
    assert most.reserve() is None
    assert most.peek() == Wait(START + GAP + JITTER, blocked=False)
    clock.now = START + GAP + JITTER
    least = make_gate(clock, rng=lambda: 0.0)
    assert least.reserve() is None
    assert least.peek() == Wait(clock.now + GAP, blocked=False)


def test_a_block_opens_the_breaker_and_doubles(make_gate):
    clock = EpochClock()
    gate = make_gate(clock)
    lengths = []
    for _ in range(4):
        until = gate.record_block()
        lengths.append((until - clock.now) / HOUR)
        assert gate.peek() == Wait(until, blocked=True)
        assert gate.reserve() == Wait(until, blocked=True)
        clock.now = until + 1  # the probe after the block: allowed, and blocked again
        assert gate.reserve() is None
    assert lengths == [6, 12, 24, 24]


def test_a_success_closes_the_breaker(make_gate):
    clock = EpochClock()
    gate = make_gate(clock)
    gate.record_block()
    clock.now += 7 * HOUR
    started = clock.now
    assert gate.reserve() is None
    gate.record_success(started)
    clock.now += GAP + JITTER
    assert gate.peek() is None
    assert gate.record_block() == clock.now + 6 * HOUR  # back to 6 hours, not 12


def test_a_newer_block_survives_an_older_success(make_gate):
    clock = EpochClock()
    gate = make_gate(clock)
    started = clock.now  # fetch A is reserved, and fetch B at the same moment
    clock.now += 5
    first = gate.record_block(started)  # B gets a 429 while A still runs ...
    clock.now += 5
    assert gate.record_block(started) == first  # ... A gets its 429 too: one block, not two
    gate.record_success(started)  # ... or A works after all: B's newer block stays
    assert gate.peek() == Wait(first, blocked=True)
    assert first == started + 5 + 6 * HOUR
    gate.record_success(clock.now)  # a fetch that started after the block closes it
    assert gate.peek() is None


# ---- Postgres only ------------------------------------------------------------------------------------


def _reserve_together(gates: list[PostgresGate]) -> list[Wait | None]:
    """Every gate reserves at the same moment, each in a thread of its own."""
    barrier = threading.Barrier(len(gates))
    results: list[Wait | None] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def ask(gate: PostgresGate) -> None:
        try:
            barrier.wait(timeout=10)
            answer = gate.reserve()
            with lock:
                results.append(answer)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=ask, args=(gate,), daemon=True) for gate in gates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads), "a reserve hung"
    assert errors == []
    return results


def test_two_engines_reserving_together_get_one_go_ahead(pg_engine: Engine):
    second = make_engine(pg_engine.url.render_as_string(hide_password=False))
    clock = EpochClock()
    gates = [_pg_gate(pg_engine, clock), _pg_gate(second, clock)]
    try:
        for round_ in range(20):
            _execute(
                pg_engine,
                "update resources set next_allowed_at = null, blocked_until = null, blocked_at = null,"
                " streak = 0 where name = 'youtube'",
            )
            results = _reserve_together(gates)
            assert results.count(None) == 1, f"round {round_}: {results}"
            assert Wait(START + GAP + 0.5 * JITTER, blocked=False) in results
    finally:
        second.dispose()


def test_a_missing_row_closes_the_gate_and_is_rewritten(pg_engine: Engine, caplog):
    clock = EpochClock()
    gate = _pg_gate(pg_engine, clock)
    _execute(pg_engine, "delete from resources where name = 'youtube'")
    with caplog.at_level(logging.ERROR, logger="catcher.youtube"):
        assert gate.reserve() == Wait(START + 6 * HOUR, blocked=True)  # never a go ahead
    assert any("missing" in record.getMessage() for record in caplog.records)
    assert _row(pg_engine) == (None, START + 6 * HOUR, START, 1, START)
    assert gate.peek() == Wait(START + 6 * HOUR, blocked=True)  # and it stays closed

    _execute(pg_engine, "delete from resources where name = 'youtube'")
    assert gate.peek() == Wait(START + 6 * HOUR, blocked=True)  # a peek closes it too
    assert _row(pg_engine) is not None

    _execute(pg_engine, "delete from resources where name = 'youtube'")
    assert gate.record_block(START) == START + 6 * HOUR  # one block, not a second one on top
    assert _row(pg_engine) == (None, START + 6 * HOUR, START, 1, START)


def test_damaged_values_close_the_gate(pg_engine: Engine, caplog):
    clock = EpochClock()
    gate = _pg_gate(pg_engine, clock)
    _execute(pg_engine, "update resources set streak = -3 where name = 'youtube'")
    with caplog.at_level(logging.ERROR, logger="catcher.youtube"):
        assert gate.reserve() == Wait(START + 6 * HOUR, blocked=True)
    assert any("damaged" in record.getMessage() for record in caplog.records)
    assert _row(pg_engine) == (None, START + 6 * HOUR, START, 1, START)

    caplog.clear()
    _execute(
        pg_engine,
        "update resources set blocked_until = :far, streak = 1 where name = 'youtube'",
        far=datetime(2100, 1, 1, tzinfo=UTC),
    )
    with caplog.at_level(logging.ERROR, logger="catcher.youtube"):
        assert gate.reserve() == Wait(START + 24 * HOUR, blocked=True)  # damage, cut to 24 hours
    assert any("24 hours" in record.getMessage() for record in caplog.records)
    assert _row(pg_engine)[1] == START + 24 * HOUR  # type: ignore[index]

    _execute(
        pg_engine,
        "update resources set blocked_until = null, blocked_at = :before where name = 'youtube'",
        before=datetime(1969, 12, 31, tzinfo=UTC),
    )
    assert gate.peek() == Wait(START + 6 * HOUR, blocked=True)  # a time before 1970 is damage


def test_microsecond_rounding_does_not_flip_the_gap(pg_engine: Engine):
    """A time that is not a whole microsecond is stored rounded up (never earlier than the rules said), so
    the gate never opens a moment too early; rounding to the nearest microsecond would."""
    now = START + 0.1234564  # rounds DOWN to the nearest microsecond
    clock = EpochClock(now)
    gate = _pg_gate(pg_engine, clock, jitter_s=0.0)
    assert gate.reserve() is None
    until = now + GAP
    clock.now = math.nextafter(until, 0.0)  # the last moment before the gap ends
    wait = gate.peek()
    assert wait is not None and not wait.blocked and wait.until >= until
    clock.now = wait.until
    assert gate.peek() is None

    clock.now = START + 1000.4999996
    blocked_until = gate.record_block()
    assert blocked_until >= clock.now + 6 * HOUR
    clock.now = math.nextafter(clock.now + 6 * HOUR, 0.0)
    assert gate.peek() == Wait(blocked_until, blocked=True)


def test_the_time_zone_of_the_session_does_not_shift_the_gap(pg_engine: Engine):
    url = pg_engine.url.render_as_string(hide_password=False)
    chatham = create_engine(url)  # +13:45, an offset no rounding to hours could hide

    @event.listens_for(chatham, "connect")
    def _set_time_zone(dbapi_connection, _record) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'Pacific/Chatham'")
        dbapi_connection.commit()

    clock = EpochClock()
    try:
        far, utc = _pg_gate(chatham, clock), _pg_gate(pg_engine, clock)
        assert far.reserve() is None
        assert _row(pg_engine)[0] == START + GAP + 0.5 * JITTER  # type: ignore[index]
        assert utc.peek() == Wait(START + GAP + 0.5 * JITTER, blocked=False)
        until = utc.record_block()
        assert far.peek() == Wait(until, blocked=True)
        assert until == START + 6 * HOUR
        with chatham.connect() as connection:
            assert connection.execute(text("show time zone")).scalar() == "Pacific/Chatham"
    finally:
        chatham.dispose()


@pytest.mark.parametrize("failure", ["closed port", "lock timeout"])
def test_a_database_error_raises_gate_unavailable_and_changes_nothing(pg_engine: Engine, failure):
    clock = EpochClock()
    _pg_gate(pg_engine, clock).record_block()
    before = _row(pg_engine)
    url = pg_engine.url.render_as_string(hide_password=False)
    if failure == "closed port":
        broken = create_engine(pg_engine.url.set(host="127.0.0.1", port=1))
    else:
        broken = create_engine(url, connect_args={"options": "-c lock_timeout=200"})
    holder = pg_engine.connect()
    try:
        if failure == "lock timeout":
            transaction = holder.begin()
            holder.execute(text("select 1 from resources where name = 'youtube' for update"))
        gate = _pg_gate(broken, clock)
        clock.now += 7 * HOUR
        for call in (gate.peek, gate.reserve, gate.record_success, gate.record_block):
            with pytest.raises(GateUnavailable) as raised:
                call()
            assert raised.value.__cause__ is not None
        if failure == "lock timeout":
            transaction.rollback()  # type: ignore[possibly-undefined]
    finally:
        holder.close()
        broken.dispose()
    assert _row(pg_engine) == before
