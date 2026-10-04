"""The rules of the YouTube gate, as pure functions: no file, no database, no clock of their own.

The file gate (`gate.py`, Stage A) and the database gate (the worker) both read their state, apply these
rules and write the result back, so the two cannot drift apart:

- **the gap:** at least `min_gap_s` (plus a random `jitter_s`) between the *start* of two fetches;
- **the breaker:** after a block, no call for `block_hours`, then twice as long, up to 24 hours, until a fetch
  works again;
- **newer news wins:** a block recorded after a fetch started is not doubled by that fetch's own block, and is
  not closed by that fetch's success;
- **damage is not a block:** a value more than 24 hours ahead is cut to 24 hours; a negative or non-finite
  value is damage (`ValueError`), and the gate then closes (`closed_state`).

Times are seconds since the epoch.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

MAX_BLOCK_HOURS = 24.0


@dataclass(frozen=True)
class Wait:
    """Why a call to YouTube is not allowed yet, and until when (seconds since the epoch)."""

    until: float
    blocked: bool  # True: the breaker is open (hours). False: just the gap between two fetches

    def message(self, now: float | None = None) -> str:
        clock = clock_text(self.until, now if now is not None else time.time())
        return f"YouTube blocked until {clock}" if self.blocked else f"YouTube: next call allowed at {clock}"


def clock_text(until: float, now: float) -> str:
    """`until` as the local time a person reads: `HH:MM` today, `YYYY-MM-DD HH:MM` on another day."""
    moment = datetime.fromtimestamp(until)
    today = datetime.fromtimestamp(now).date()
    return moment.strftime("%H:%M") if moment.date() == today else moment.strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class GateState:
    """What the gate remembers. All 0 is open and never blocked."""

    next_allowed_at: float  # the gap: no fetch may start before this
    blocked_until: float  # the breaker: no fetch before this
    blocked_at: float  # when the last block was recorded
    streak: int  # blocks in a row since the last fetch that worked


OPEN = GateState(next_allowed_at=0.0, blocked_until=0.0, blocked_at=0.0, streak=0)


def wait_for(state: GateState, now: float) -> Wait | None:
    """None when a call is allowed now; else the wait, a block first, then the gap."""
    if now < state.blocked_until:
        return Wait(state.blocked_until, blocked=True)
    if now < state.next_allowed_at:
        return Wait(state.next_allowed_at, blocked=False)
    return None


def after_reserve(
    state: GateState, now: float, *, min_gap_s: float, jitter_s: float, rng: Callable[[], float]
) -> GateState:
    """A fetch starts now: the gap to the next one starts with it."""
    return replace(state, next_allowed_at=now + min_gap_s + rng() * jitter_s)


def after_success(state: GateState, started_at: float | None) -> GateState:
    """A fetch worked: the breaker closes, unless a block was recorded after that fetch started."""
    if started_at is not None and state.blocked_at > started_at:
        return state
    return replace(state, streak=0, blocked_until=0.0)


def block_length_hours(streak: int, block_hours: float) -> float:
    """How long block number `streak` lasts: `block_hours`, then twice as long, up to 24 hours. The exponent
    stops at 16 (far past 24 hours), so a damaged huge streak never builds a huge number."""
    return min(block_hours * 2 ** min(streak - 1, 16), MAX_BLOCK_HOURS)


def after_block(state: GateState, now: float, started_at: float | None, *, block_hours: float) -> GateState:
    """YouTube said no to a fetch that started at `started_at`. Two fetches that were running together and
    both get the no are one block, not two: a block recorded after that start that still runs stays as it is
    (the same state object is returned)."""
    if started_at is not None and state.blocked_at >= started_at and state.blocked_until > now:
        return state
    streak = state.streak + 1
    hours = block_length_hours(streak, block_hours)
    return replace(state, streak=streak, blocked_until=now + hours * 3600, blocked_at=now)


def clamp(state: GateState, now: float) -> GateState:
    """Check a state that was read back. A negative or non-finite value raises `ValueError` (damage); a gap
    or a block more than 24 hours ahead is cut to 24 hours (damage too, not a block)."""
    for key in ("next_allowed_at", "blocked_until", "blocked_at", "streak"):
        value = getattr(state, key)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} is {value!r}")
    ceiling = now + MAX_BLOCK_HOURS * 3600
    return replace(
        state,
        blocked_until=min(state.blocked_until, ceiling),
        next_allowed_at=min(state.next_allowed_at, ceiling),
    )


def closed_state(now: float, block_hours: float) -> GateState:
    """The state of a gate that failed closed (damaged or missing state): blocked for `block_hours`, at least
    one hour, as a first block."""
    hours = max(block_hours, 1.0)
    return GateState(next_allowed_at=0.0, blocked_until=now + hours * 3600, blocked_at=now, streak=1)
