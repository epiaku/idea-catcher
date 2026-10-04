"""The YouTube gate kept in Postgres: the row `youtube` in `resources`, for the worker.

The same rules as the file gate (`gate_rules.py`), on a row that every worker shares. Each call is one short
transaction of its own: take the row with `SELECT ... FOR UPDATE`, apply a rule, write, commit. The row lock
is what lets exactly one of two workers asking at the same moment go ahead.

It fails **closed**, like the file gate:

- a **missing** row is inserted closed (blocked for `block_hours`) and an error is logged;
- **damaged** values (negative, before 1970, a negative streak) rewrite the row closed and log an error; a
  gap or a block more than 24 hours ahead is cut to 24 hours (damage too, not a block) and logged;
- a **database error** (down, a lock timeout) raises `GateUnavailable` and changes nothing: the caller must
  not fetch.

Times are seconds since the epoch in Python and `timestamptz` in the row, converted only here, at the edge.
A row keeps whole microseconds, so every time is stored rounded *up*: a gap or a block can last a
microsecond longer than the rules say but never ends a moment too early, and a block recorded in the same
microsecond as the start of a fetch counts as newer (it stays). After reading, times are compared as floats.
"""

import logging
import math
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from fractions import Fraction

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from catcher.core.db import session_scope
from catcher.modules.queue.models import Resource
from catcher.modules.youtube.gate_rules import (
    GateState,
    Wait,
    after_block,
    after_reserve,
    after_success,
    block_length_hours,
    clamp,
    closed_state,
    wait_for,
)

__all__ = ["GateUnavailable", "PostgresGate"]

log = logging.getLogger("catcher.youtube")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_US = 1_000_000


class GateUnavailable(RuntimeError):
    """The gate could not read or write its row (the database is down, a lock timed out): do not fetch."""


def _floor_us(seconds: float) -> int:
    return math.floor(Fraction(seconds) * _US)  # exact: no float rounding on the way


def _ceil_us(seconds: float) -> int:
    return math.ceil(Fraction(seconds) * _US)


def _moment(us: int) -> datetime | None:
    """Whole microseconds since the epoch as an aware UTC time; 0 ("never") is NULL in the row."""
    return None if us == 0 else _EPOCH + timedelta(microseconds=us)


def _seconds(moment: datetime | None) -> float:
    """An aware time from the row (in any session time zone) as seconds since the epoch; NULL is 0."""
    return 0.0 if moment is None else moment.timestamp()


class PostgresGate:
    def __init__(
        self,
        engine: Engine,
        *,
        name: str = "youtube",
        min_gap_s: float = 120.0,
        jitter_s: float = 300.0,
        block_hours: float = 6.0,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.engine = engine
        self.name = name
        self.min_gap_s = max(0.0, min_gap_s)
        self.jitter_s = max(0.0, jitter_s)
        self.block_hours = max(0.0, block_hours)
        self.clock = clock
        self.rng = rng

    # ---- the row ----------------------------------------------------------------------------------

    def _select(self, session: Session) -> Resource | None:
        return session.execute(
            select(Resource).where(Resource.name == self.name).with_for_update()
        ).scalar_one_or_none()

    @staticmethod
    def _columns(state: GateState) -> dict[str, object]:
        """Every time rounded up to the microsecond: a gap or a block never ends before the rules said."""
        return {
            "next_allowed_at": _moment(_ceil_us(state.next_allowed_at)),
            "blocked_until": _moment(_ceil_us(state.blocked_until)),
            "blocked_at": _moment(_ceil_us(state.blocked_at)),
            "streak": state.streak,
        }

    def _write(self, row: Resource, state: GateState, now: float) -> GateState:
        """Write `state` into the locked row and return it as the row now holds it."""
        for key, value in self._columns(state).items():
            setattr(row, key, value)
        row.updated_at = _EPOCH + timedelta(microseconds=_floor_us(now))
        return self._state(row)

    @staticmethod
    def _state(row: Resource) -> GateState:
        return GateState(
            next_allowed_at=_seconds(row.next_allowed_at),
            blocked_until=_seconds(row.blocked_until),
            blocked_at=_seconds(row.blocked_at),
            streak=row.streak,
        )

    def _read(self, session: Session, now: float) -> tuple[Resource, GateState]:
        """Lock the row and read it, closing the gate over a missing row or damaged values."""
        row = self._select(session)
        if row is None:
            hours = max(self.block_hours, 1.0)
            closed = closed_state(now, self.block_hours)
            session.execute(
                insert(Resource)
                .values(
                    name=self.name,
                    concurrency=1,
                    updated_at=_EPOCH + timedelta(microseconds=_floor_us(now)),
                    **self._columns(closed),
                )
                .on_conflict_do_nothing(index_elements=[Resource.name])
            )
            log.error(
                "the YouTube gate row %r is missing: inserted closed, no calls for %g hours to be safe",
                self.name,
                hours,
            )
            row = self._select(session)  # ours, or the one another worker inserted at the same moment
            if row is None:
                raise GateUnavailable(f"the YouTube gate row {self.name!r} could not be inserted")
        raw = self._state(row)
        try:
            state = clamp(raw, now)
        except ValueError as e:
            log.error(
                "the YouTube gate row %r is damaged (%s): rewritten closed, no calls for %g hours to be safe",
                self.name,
                e,
                max(self.block_hours, 1.0),
            )
            return row, self._write(row, closed_state(now, self.block_hours), now)
        if state != raw:
            log.error(
                "the YouTube gate row %r is damaged (a time more than 24 hours ahead): cut to 24 hours",
                self.name,
            )
            state = self._write(row, state, now)
        return row, state

    def _transaction[T](self, work: Callable[[Session, float], T]) -> T:
        """One short transaction; any database error is `GateUnavailable`, and nothing is changed."""
        try:
            with session_scope(self.engine) as session:
                return work(session, self.clock())
        except SQLAlchemyError as e:
            raise GateUnavailable(f"the YouTube gate row {self.name!r} is unavailable: {e}") from e

    # ---- the gate ---------------------------------------------------------------------------------

    def peek(self) -> Wait | None:
        """Is a call allowed now? Changes nothing (apart from closing the gate over a damaged row)."""

        def work(session: Session, now: float) -> Wait | None:
            return wait_for(self._read(session, now)[1], now)

        return self._transaction(work)

    def reserve(self) -> Wait | None:
        """Ask for a call. None means go ahead, and the gap to the next call has started."""

        def work(session: Session, now: float) -> Wait | None:
            row, state = self._read(session, now)
            wait = wait_for(state, now)
            if wait is not None:
                return wait
            self._write(
                row,
                after_reserve(state, now, min_gap_s=self.min_gap_s, jitter_s=self.jitter_s, rng=self.rng),
                now,
            )
            return None

        return self._transaction(work)

    def record_success(self, started_at: float | None = None) -> None:
        """A fetch worked: close the breaker. `started_at` is when that fetch was reserved: a block recorded
        after it (by another worker) is newer news and stays."""

        def work(session: Session, now: float) -> None:
            row, state = self._read(session, now)
            new = after_success(state, started_at)
            if new is state:
                return
            if state.streak or state.blocked_until:
                log.info("YouTube answered again: the breaker is closed")
            self._write(row, new, now)

        self._transaction(work)

    def record_block(self, started_at: float | None = None) -> float:
        """YouTube said no. Open the breaker (6 hours, then 12, then 24) and return when it ends.

        Two fetches that were running together and both get the no are one block, not two: when a block was
        recorded after this fetch started, the breaker stays as it is."""

        def work(session: Session, now: float) -> float:
            row, state = self._read(session, now)
            new = after_block(state, now, started_at, block_hours=self.block_hours)
            if new is state:
                return state.blocked_until
            stored = self._write(row, new, now)
            log.error(
                "YouTube is blocking us (block %d): no calls for %g hours, until %s",
                stored.streak,
                block_length_hours(stored.streak, self.block_hours),
                datetime.fromtimestamp(stored.blocked_until).strftime("%Y-%m-%d %H:%M"),
            )
            return stored.blocked_until

        return self._transaction(work)
