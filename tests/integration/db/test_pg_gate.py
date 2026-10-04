"""The YouTube gate in Postgres: the same behaviour as the in-memory test gate (a contract suite run on
both), plus what only a shared database brings (two workers at once, a missing or damaged row, time zones,
errors)."""

import logging
import math
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from memory_gate import InMemoryGate
from sqlalchemy import Engine, create_engine, event, text

from catcher.core.db import make_engine, make_worker_engine
from catcher.modules.youtube.gate import Gate, Wait
from catcher.modules.youtube.gate_rules import GateState
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


@pytest.fixture(params=["memory", "postgres"])
def make_gate(request, pg_engine: Engine) -> MakeGate:
    """A factory for the gate under test; every gate it makes shares one state (the in-memory state or the
    row). In memory one state is one gate: a further call returns that same gate, so the state carries over
    (two gate objects over one state, as two workers on the row), with the clock, rng and hours of the
    latest call."""
    memory: list[InMemoryGate] = []

    def make(clock: EpochClock, *, rng: Callable[[], float] = lambda: 0.5, hours: float = 6.0) -> Gate:
        options = {"min_gap_s": GAP, "jitter_s": JITTER, "block_hours": hours, "clock": clock, "rng": rng}
        if request.param == "memory":
            if not memory:
                memory.append(InMemoryGate(**options))
            gate = memory[0]
            gate.clock, gate.rng, gate.block_hours = clock, rng, hours
            return gate
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


# ---- the contract: the in-memory gate and the Postgres gate behave the same -------------------------


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


def test_a_block_time_far_ahead_is_repaired_to_now(pg_engine: Engine, caplog):
    """A `blocked_at` more than 24 hours ahead is damage: cut to now and written back, so a success can close
    the breaker again. The block itself and the streak stay."""
    clock = EpochClock()
    gate = _pg_gate(pg_engine, clock)
    _execute(
        pg_engine,
        "update resources set blocked_until = :until, blocked_at = :far, streak = 2 where name = 'youtube'",
        until=datetime.fromtimestamp(START + HOUR, UTC),
        far=datetime(2100, 1, 1, tzinfo=UTC),
    )
    with caplog.at_level(logging.ERROR, logger="catcher.youtube"):
        assert gate.peek() == Wait(START + HOUR, blocked=True)
    assert any("damaged" in record.getMessage() for record in caplog.records)
    assert _row(pg_engine)[1:4] == (START + HOUR, START, 2)  # type: ignore[index]

    clock.now = START + HOUR  # the block is over: a fetch that works closes the breaker
    assert gate.reserve() is None
    gate.record_success(clock.now)
    assert _row(pg_engine)[1:4] == (None, START, 0)  # type: ignore[index]


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
def test_a_database_error_raises_gate_unavailable_and_changes_nothing(
    pg_engine: Engine, failure, monkeypatch
):
    clock = EpochClock()
    _pg_gate(pg_engine, clock).record_block()
    before = _row(pg_engine)
    url = pg_engine.url.render_as_string(hide_password=False)
    if failure == "closed port":
        broken = create_engine(pg_engine.url.set(host="127.0.0.1", port=1))
    else:
        monkeypatch.setattr("catcher.core.db.WORKER_LOCK_TIMEOUT_MS", 200)  # the worker's engine, sooner
        broken = make_worker_engine(url)
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


def _wait_until_a_session_waits_for_a_lock(engine: Engine) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.execute(
                text(
                    "select count(*) from pg_stat_activity"
                    " where wait_event_type = 'Lock' and datname = current_database()"
                )
            ).scalar()
        if waiting:
            return
        time.sleep(0.01)
    raise AssertionError("the gate never waited for the lock")


def _in_thread(call: Callable[[], object]) -> Callable[[], object]:
    """Start `call` in a daemon thread; the returned function joins it and gives its result (or raises)."""
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = call()
        except BaseException as error:
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def join() -> object:
        thread.join(timeout=30)
        assert not thread.is_alive(), "the gate call hung"
        if "error" in outcome:
            raise outcome["error"]  # type: ignore[misc]
        return outcome.get("result")

    return join


@pytest.mark.parametrize("call", ["reserve", "record_block"])
def test_the_clock_is_read_under_the_row_lock(pg_engine: Engine, caplog, call):
    """A call that waited for the lock uses the time it got the lock, not the time it asked: the gap starts
    then, the block is dated then, and a 24 hour block of the other worker is not taken for damage."""
    clock = EpochClock()
    second = make_engine(pg_engine.url.render_as_string(hide_password=False))
    waited = 300.0
    try:
        with pg_engine.connect() as holder:
            transaction = holder.begin()
            holder.execute(text("select 1 from resources where name = 'youtube' for update"))
            gate = _pg_gate(second, clock)
            join = _in_thread(gate.reserve if call == "reserve" else lambda: gate.record_block(START))
            _wait_until_a_session_waits_for_a_lock(pg_engine)
            clock.now += waited  # time passes while the call waits for the lock ...
            if call == "record_block":  # ... and the other worker records its 3rd block: 24 hours
                holder.execute(
                    text(
                        "update resources set blocked_until = :until, blocked_at = :at, streak = 3"
                        " where name = 'youtube'"
                    ),
                    {
                        "until": datetime.fromtimestamp(clock.now + 24 * HOUR, UTC),
                        "at": datetime.fromtimestamp(clock.now, UTC),
                    },
                )
            with caplog.at_level(logging.ERROR, logger="catcher.youtube"):
                transaction.commit()
                result = join()
    finally:
        second.dispose()
    assert not any("damaged" in record.getMessage() for record in caplog.records)
    row = _row(pg_engine)
    if call == "reserve":
        assert result is None
        assert row[0] == START + waited + GAP + 0.5 * JITTER  # type: ignore[index]
    else:
        assert result == START + waited + 24 * HOUR  # the other worker's block: one block, kept whole
        assert row[1:4] == (START + waited + 24 * HOUR, START + waited, 3)  # type: ignore[index]


@pytest.mark.parametrize("column", ["next_allowed_at", "blocked_until", "blocked_at"])
@pytest.mark.parametrize("value", ["infinity", "-infinity", "20000-01-01 00:00:00+00"])
def test_an_unreadable_time_is_damage_and_is_repaired(pg_engine: Engine, caplog, column, value):
    """Python cannot hold 'infinity' or a year past 9999: the gate must repair the row, not fail forever."""
    clock = EpochClock()
    gate = _pg_gate(pg_engine, clock)
    _execute(pg_engine, f"update resources set {column} = cast(:value as timestamptz)", value=value)
    with caplog.at_level(logging.ERROR, logger="catcher.youtube"):
        assert gate.reserve() == Wait(START + 6 * HOUR, blocked=True)
    assert any("damaged" in record.getMessage() for record in caplog.records)
    assert _row(pg_engine) == (None, START + 6 * HOUR, START, 1, START)
    assert gate.peek() == Wait(START + 6 * HOUR, blocked=True)


def test_a_row_another_worker_inserted_first_is_read_not_reported(pg_engine: Engine, caplog):
    """Two gates find the row missing at once: only the one whose insert inserted logs the error."""
    clock = EpochClock()
    _execute(pg_engine, "delete from resources where name = 'youtube'")
    second = make_engine(pg_engine.url.render_as_string(hide_password=False))
    try:
        with pg_engine.connect() as other:
            transaction = other.begin()
            other.execute(  # the other worker inserted the row and has not committed yet
                text(
                    "insert into resources (name, streak, concurrency, updated_at)"
                    " values ('youtube', 0, 1, :at)"
                ),
                {"at": datetime.fromtimestamp(START, UTC)},
            )
            with caplog.at_level(logging.ERROR, logger="catcher.youtube"):
                join = _in_thread(_pg_gate(second, clock).peek)
                _wait_until_a_session_waits_for_a_lock(pg_engine)  # our insert waits on its key
                transaction.commit()
                result = join()
    finally:
        second.dispose()
    assert not any("missing" in record.getMessage() for record in caplog.records)
    assert result is None  # the row the other worker inserted is read and used


# ---- snapshot and import_state (for `catcher youtube gate`) ---------------------------------------------


def test_snapshot_reads_the_row_and_changes_nothing(pg_engine: Engine):
    clock = EpochClock()
    gate = _pg_gate(pg_engine, clock)
    assert gate.snapshot() == GateState(0.0, 0.0, 0.0, 0)
    assert gate.reserve() is None
    until = gate.record_block()
    before = _row(pg_engine)
    clock.now += 60
    assert gate.snapshot() == GateState(START + GAP + 0.5 * JITTER, until, START, 1)
    assert _row(pg_engine) == before


def test_snapshot_closes_a_missing_or_damaged_row(pg_engine: Engine, caplog):
    clock = EpochClock()
    gate = _pg_gate(pg_engine, clock)
    _execute(pg_engine, "delete from resources where name = 'youtube'")
    with caplog.at_level(logging.ERROR, logger="catcher.youtube"):
        assert gate.snapshot() == GateState(0.0, START + 6 * HOUR, START, 1)
    assert any("missing" in record.getMessage() for record in caplog.records)
    assert _row(pg_engine) == (None, START + 6 * HOUR, START, 1, START)

    caplog.clear()
    _execute(pg_engine, "update resources set blocked_until = 'infinity' where name = 'youtube'")
    clock.now += 60
    with caplog.at_level(logging.ERROR, logger="catcher.youtube"):
        assert gate.snapshot() == GateState(0.0, START + 60 + 6 * HOUR, START + 60, 1)
    assert any("damaged" in record.getMessage() for record in caplog.records)


def test_import_state_writes_the_state_into_the_row(pg_engine: Engine):
    clock = EpochClock()
    gate = _pg_gate(pg_engine, clock)
    imported = GateState(START + 100, START + 12 * HOUR, START - 60, 2)
    assert gate.import_state(imported) is True
    assert _row(pg_engine) == (START + 100, START + 12 * HOUR, START - 60, 2, START)
    assert gate.snapshot() == imported
    assert gate.peek() == Wait(START + 12 * HOUR, blocked=True)


def test_import_state_never_shortens_a_longer_block(pg_engine: Engine):
    clock = EpochClock()
    gate = _pg_gate(pg_engine, clock)
    until = gate.record_block()  # 6 hours
    before = _row(pg_engine)
    assert gate.import_state(GateState(0.0, START + 2 * HOUR, START, 1)) is False
    assert gate.import_state(GateState(0.0, 0.0, 0.0, 0)) is False  # an open file does not open the row
    assert _row(pg_engine) == before

    assert gate.import_state(GateState(0.0, START + 12 * HOUR, START, 2)) is True  # a longer one is kept
    assert gate.snapshot().blocked_until == START + 12 * HOUR > until

    clock.now += 13 * HOUR  # the row's block is over, but an open file still resets nothing (streak kept)
    assert gate.import_state(GateState(0.0, 0.0, 0.0, 0)) is False
    assert gate.snapshot() == GateState(0.0, START + 12 * HOUR, START, 2)


def test_import_state_never_shortens_the_gap(pg_engine: Engine):
    clock = EpochClock()
    gate = _pg_gate(pg_engine, clock)
    assert gate.reserve() is None
    gap_until = START + GAP + 0.5 * JITTER
    assert gate.import_state(GateState(START + 10, 0.0, 0.0, 0)) is False  # the row covers it
    assert gate.snapshot().next_allowed_at == gap_until
    assert gate.import_state(GateState(gap_until + 50, 0.0, 0.0, 0)) is True
    assert gate.snapshot().next_allowed_at == gap_until + 50


def test_snapshot_and_import_state_raise_gate_unavailable_when_the_database_is_down(pg_engine: Engine):
    broken = create_engine(pg_engine.url.set(host="127.0.0.1", port=1))
    try:
        gate = _pg_gate(broken, EpochClock())
        for call in (gate.snapshot, lambda: gate.import_state(GateState(0.0, 0.0, 0.0, 0))):
            with pytest.raises(GateUnavailable):
                call()
    finally:
        broken.dispose()


def test_import_state_keeps_the_streak_of_an_expired_block(pg_engine: Engine):
    """Blocked three times, the last block just over, no fetch worked since: the next block must be 24 hours.
    An open or older file must not reset the streak (the next 429 would give only 6 hours)."""
    clock = EpochClock()
    gate = _pg_gate(pg_engine, clock)
    _execute(
        pg_engine,
        "update resources set blocked_until = :until, blocked_at = :at, streak = 3 where name = 'youtube'",
        until=datetime.fromtimestamp(START - HOUR, UTC),
        at=datetime.fromtimestamp(START - 25 * HOUR, UTC),
    )
    before = _row(pg_engine)
    assert gate.import_state(GateState(0.0, 0.0, 0.0, 0)) is False
    assert gate.import_state(GateState(0.0, START - 2 * HOUR, START - 30 * HOUR, 1)) is False
    assert _row(pg_engine) == before
    assert gate.snapshot().streak == 3
    assert gate.record_block() == START + 24 * HOUR  # block 4: 24 hours, not a first block of 6


def test_import_state_keeps_a_newer_blocked_at(pg_engine: Engine):
    """A longer file block with an older record time lengthens the block, but keeps the row's newer
    `blocked_at`: a fetch that started between the two and then worked must not close the block."""
    clock = EpochClock()
    gate = _pg_gate(pg_engine, clock)
    gate.record_block()  # until START + 6 h, recorded at START, block 1
    assert gate.import_state(GateState(0.0, START + 8 * HOUR, START - 2 * HOUR, 1)) is True
    assert gate.snapshot() == GateState(0.0, START + 8 * HOUR, START, 1)

    gate.record_success(started_at=START - HOUR)  # started before our newer block: older news
    assert gate.peek() == Wait(START + 8 * HOUR, blocked=True)


def test_importing_the_same_state_twice_changes_nothing_the_second_time(pg_engine: Engine):
    clock = EpochClock()
    gate = _pg_gate(pg_engine, clock)
    imported = GateState(
        START + 100.1234567, START + 12 * HOUR + 0.5, START - 60, 2
    )  # not whole microseconds
    assert gate.import_state(imported) is True
    after_first = _row(pg_engine)
    clock.now += 5
    assert gate.import_state(imported) is False
    assert _row(pg_engine) == after_first  # updated_at too: nothing was written
