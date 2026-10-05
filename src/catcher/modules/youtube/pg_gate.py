"""The YouTube gate kept in Postgres: the row `youtube` in `resources`, the one gate of every command.

The shared rules (`gate_rules.py`), on a row that every worker and command shares. Each call is one short
transaction of its own: take the row with `SELECT ... FOR UPDATE`, apply a rule, write, commit. The row lock
is what lets exactly one of two workers asking at the same moment go ahead.

It fails **closed**:

- a **missing** row is inserted closed (blocked for `block_hours`) and an error is logged;
- **damaged** values (negative, before 1970, 'infinity', past the year 9999, a negative streak) rewrite the
  row closed and log an error; a gap or a block more than 24 hours ahead is cut to 24 hours (damage too, not
  a block), a block time more than 24 hours ahead is cut to now, and that is logged;
- a **database error** (down, a lock timeout) raises `GateUnavailable` and changes nothing: the caller must
  not fetch.

The clock is read once the row lock is held: a call that waited for the lock uses the time it got it. Times
are seconds since the epoch in Python and `timestamptz` in the row, converted only here, at the edge (read
with `extract(epoch ...)`, so any stored value can be read).
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
from decimal import Decimal
from fractions import Fraction

from sqlalchemy import Engine, Numeric, Row, extract, select, type_coerce, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from catcher.core.db import session_scope
from catcher.modules.queue.models import Resource
from catcher.modules.youtube.gate import GateUnavailable
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

# The gate's row in `resources`, and the `resource` every `youtube.fetch` job carries (the claim skips those
# jobs while this row is closed). Migration 0004 seeds the row under the same name, as a literal.
YOUTUBE_RESOURCE = "youtube"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_US = 1_000_000
_LAST_SECOND = (datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC) - _EPOCH).total_seconds()
_TIMES = ("next_allowed_at", "blocked_until", "blocked_at")


def _floor_us(seconds: float) -> int:
    return math.floor(Fraction(seconds) * _US)  # exact: no float rounding on the way


def _ceil_us(seconds: float) -> int:
    return math.ceil(Fraction(seconds) * _US)


def _moment(us: int) -> datetime | None:
    """Whole microseconds since the epoch as an aware UTC time; 0 ("never") is NULL in the row."""
    return None if us == 0 else _EPOCH + timedelta(microseconds=us)


def _seconds(moment: datetime | None) -> float:
    return 0.0 if moment is None else moment.timestamp()


def _epoch(key: str, value: Decimal | None) -> float:
    """A time read as `extract(epoch ...)` (the same in any session time zone); NULL is 0. 'infinity' or a
    time past the year 9999 is damage (`ValueError`); a negative one is left to `clamp`."""
    if value is None:
        return 0.0
    seconds = float(value)
    if not math.isfinite(seconds) or seconds > _LAST_SECOND:
        raise ValueError(f"{key} is {value}")
    return seconds


class PostgresGate:
    def __init__(
        self,
        engine: Engine,
        *,
        name: str = YOUTUBE_RESOURCE,
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

    def _select(self, session: Session) -> Row | None:
        # `extract` is typed Integer in SQLAlchemy; Postgres gives a numeric with the fraction of a second
        columns = [type_coerce(extract("epoch", getattr(Resource, key)), Numeric) for key in _TIMES]
        return session.execute(
            select(*columns, Resource.streak).where(Resource.name == self.name).with_for_update()
        ).first()

    @staticmethod
    def _columns(state: GateState) -> dict[str, object]:
        """Every time rounded up to the microsecond: a gap or a block never ends before the rules said."""
        return {
            "next_allowed_at": _moment(_ceil_us(state.next_allowed_at)),
            "blocked_until": _moment(_ceil_us(state.blocked_until)),
            "blocked_at": _moment(_ceil_us(state.blocked_at)),
            "streak": state.streak,
        }

    @staticmethod
    def _updated_at(now: float) -> datetime:
        return _EPOCH + timedelta(microseconds=_floor_us(now))

    def _write(self, session: Session, state: GateState, now: float) -> GateState:
        """Write `state` into the locked row and return it as the row now holds it."""
        columns = self._columns(state)
        session.execute(
            update(Resource)
            .where(Resource.name == self.name)
            .values(**columns, updated_at=self._updated_at(now))
        )
        return GateState(
            **{key: _seconds(columns[key]) for key in _TIMES},  # type: ignore[arg-type]
            streak=state.streak,
        )

    def _insert_closed(self, session: Session) -> None:
        """The row is missing: insert it closed. When another worker inserted it at the same moment, nothing
        is inserted here and nothing is logged; its row is read next."""
        now = self.clock()
        inserted = session.execute(
            insert(Resource)
            .values(
                name=self.name,
                concurrency=1,
                updated_at=self._updated_at(now),
                **self._columns(closed_state(now, self.block_hours)),
            )
            .on_conflict_do_nothing(index_elements=[Resource.name])
            .returning(Resource.name)
        ).first()
        if inserted is not None:
            log.error(
                "the YouTube gate row %r is missing: inserted closed, no calls for %g hours to be safe",
                self.name,
                max(self.block_hours, 1.0),
            )

    def _read(self, session: Session) -> tuple[GateState, float]:
        """Lock the row, then read the clock, then the row, closing the gate over a missing row or damage."""
        found = self._select(session)
        if found is None:
            self._insert_closed(session)
            found = self._select(session)  # ours, or the one another worker inserted at the same moment
            if found is None:
                raise GateUnavailable(f"the YouTube gate row {self.name!r} could not be inserted")
        now = self.clock()  # under the row lock: a call that waited for it uses the time it got it
        try:
            raw = GateState(
                **{key: _epoch(key, value) for key, value in zip(_TIMES, found[:3], strict=True)},
                streak=found[3],
            )
            state = clamp(raw, now)
        except ValueError as e:
            log.error(
                "the YouTube gate row %r is damaged (%s): rewritten closed, no calls for %g hours to be safe",
                self.name,
                e,
                max(self.block_hours, 1.0),
            )
            return self._write(session, closed_state(now, self.block_hours), now), now
        if state != raw:
            log.error(
                "the YouTube gate row %r is damaged (a time more than 24 hours ahead): an end cut to 24"
                " hours, a block time cut to now",
                self.name,
            )
            state = self._write(session, state, now)
        return state, now

    def _transaction[T](self, work: Callable[[Session], T]) -> T:
        """One short transaction; any database error is `GateUnavailable`, and nothing is changed."""
        try:
            with session_scope(self.engine) as session:
                return work(session)
        except SQLAlchemyError as e:
            raise GateUnavailable(f"the YouTube gate row {self.name!r} is unavailable: {e}") from e

    # ---- the gate ---------------------------------------------------------------------------------

    def peek(self) -> Wait | None:
        """Is a call allowed now? Changes nothing (apart from closing the gate over a damaged row)."""

        def work(session: Session) -> Wait | None:
            return wait_for(*self._read(session))

        return self._transaction(work)

    def reserve(self) -> Wait | None:
        """Ask for a call. None means go ahead, and the gap to the next call has started."""

        def work(session: Session) -> Wait | None:
            state, now = self._read(session)
            wait = wait_for(state, now)
            if wait is not None:
                return wait
            self._write(
                session,
                after_reserve(state, now, min_gap_s=self.min_gap_s, jitter_s=self.jitter_s, rng=self.rng),
                now,
            )
            return None

        return self._transaction(work)

    def record_success(self, started_at: float | None = None) -> None:
        """A fetch worked: close the breaker. `started_at` is when that fetch was reserved: a block recorded
        after it (by another worker) is newer news and stays."""

        def work(session: Session) -> None:
            state, now = self._read(session)
            new = after_success(state, started_at)
            if new is state:
                return
            if state.streak or state.blocked_until:
                log.info("YouTube answered again: the breaker is closed")
            self._write(session, new, now)

        self._transaction(work)

    def record_block(self, started_at: float | None = None) -> float:
        """YouTube said no. Open the breaker (6 hours, then 12, then 24) and return when it ends.

        Two fetches that were running together and both get the no are one block, not two: when a block was
        recorded after this fetch started, the breaker stays as it is."""

        def work(session: Session) -> float:
            state, now = self._read(session)
            new = after_block(state, now, started_at, block_hours=self.block_hours)
            if new is state:
                return state.blocked_until
            stored = self._write(session, new, now)
            log.error(
                "YouTube is blocking us (block %d): no calls for %g hours, until %s",
                stored.streak,
                block_length_hours(stored.streak, self.block_hours),
                datetime.fromtimestamp(stored.blocked_until).strftime("%Y-%m-%d %H:%M"),
            )
            return stored.blocked_until

        return self._transaction(work)

    # ---- for a person: `catcher youtube gate` ----------------------------------------------------

    def snapshot(self) -> GateState:
        """The state as the row holds it now. Changes nothing (apart from closing the gate over a missing or
        damaged row, as every other call does)."""

        def work(session: Session) -> GateState:
            return self._read(session)[0]

        return self._transaction(work)
