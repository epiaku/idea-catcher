"""The in-memory test double of the YouTube gate: the same rules as the Postgres gate, without a database."""

import logging

from memory_gate import InMemoryGate

from catcher.modules.youtube.gate import Gate, Wait
from catcher.modules.youtube.gate_rules import OPEN, GateState, after_block, after_reserve, after_success

START = 1_700_000_000.0
HOUR = 3600.0
GAP = 120.0
JITTER = 300.0


class Clock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make(clock: Clock, **options) -> InMemoryGate:
    values = {"min_gap_s": GAP, "jitter_s": JITTER, "block_hours": 6.0, "rng": lambda: 0.5} | options
    return InMemoryGate(clock=clock, **values)


def test_a_new_gate_is_open():
    gate: Gate = make(Clock())
    assert gate.peek() is None
    assert make(Clock()).state == OPEN


def test_reserve_then_the_gap_holds_a_second_call():
    clock = Clock()
    gate = make(clock)
    assert gate.reserve() is None
    until = START + GAP + 0.5 * JITTER
    assert gate.reserve() == Wait(until, blocked=False)
    assert gate.peek() == Wait(until, blocked=False)
    clock.now = until  # the gap ends at its moment
    assert gate.reserve() is None


def test_a_block_opens_the_breaker_and_doubles(caplog):
    clock = Clock()
    gate = make(clock)
    lengths = []
    with caplog.at_level(logging.ERROR, logger="catcher.youtube"):
        for _ in range(4):
            until = gate.record_block()
            lengths.append((until - clock.now) / HOUR)
            assert gate.peek() == Wait(until, blocked=True)
            assert gate.reserve() == Wait(until, blocked=True)
            clock.now = until + 1  # the probe after the block: allowed, and blocked again
            assert gate.reserve() is None
    assert lengths == [6, 12, 24, 24]
    assert sum("YouTube is blocking us" in r.getMessage() for r in caplog.records) == 4


def test_a_success_closes_the_breaker(caplog):
    clock = Clock()
    gate = make(clock)
    gate.record_block()
    clock.now += 7 * HOUR
    started = clock.now
    assert gate.reserve() is None
    with caplog.at_level(logging.INFO, logger="catcher.youtube"):
        gate.record_success(started)
    assert any("the breaker is closed" in r.getMessage() for r in caplog.records)
    clock.now += GAP + JITTER
    assert gate.peek() is None
    assert gate.record_block() == clock.now + 6 * HOUR  # back to 6 hours, not 12


def test_a_newer_block_survives_an_older_success():
    clock = Clock()
    gate = make(clock)
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


def test_state_can_be_set_by_a_test():
    clock = Clock()
    gate = make(clock)
    gate.state = GateState(next_allowed_at=0.0, blocked_until=START + HOUR, blocked_at=START, streak=2)
    assert gate.reserve() == Wait(START + HOUR, blocked=True)
    clock.now = START + HOUR
    assert gate.reserve() is None
    assert gate.state.next_allowed_at == START + HOUR + GAP + 0.5 * JITTER
    assert gate.record_block() == clock.now + 24 * HOUR  # the streak set by the test counts: block 3


def test_the_same_calls_give_the_same_state_as_the_rules():
    clock = Clock()
    gate = make(clock, rng=lambda: 0.25)
    expected = OPEN

    def rng() -> float:
        return 0.25

    gate.reserve()
    expected = after_reserve(expected, clock.now, min_gap_s=GAP, jitter_s=JITTER, rng=rng)
    started = clock.now
    clock.now += 30
    gate.record_block(started)
    expected = after_block(expected, clock.now, started, block_hours=6.0)
    clock.now += 10
    gate.record_block(started)
    expected = after_block(expected, clock.now, started, block_hours=6.0)
    gate.record_success(started)
    expected = after_success(expected, started)
    clock.now += 7 * HOUR
    later = clock.now
    gate.reserve()
    expected = after_reserve(expected, clock.now, min_gap_s=GAP, jitter_s=JITTER, rng=rng)
    gate.record_success(later)
    expected = after_success(expected, later)
    assert gate.state == expected
    assert expected.streak == 0 and expected.blocked_at == START + 30
