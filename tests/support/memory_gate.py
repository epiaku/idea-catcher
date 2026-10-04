"""An in-memory YouTube gate for the tests that run without Postgres (unit and component tests).

It implements the `Gate` protocol with the shared rules (`gate_rules.py`), exactly as `PostgresGate` applies
them, minus the database: the state is the public attribute `state`, which a test can read and set. The
clock is read once per call, as the Postgres gate reads it once it holds the row lock, and the breaker
messages are logged with the same text on the same logger.

What only a database brings (a missing or damaged row, `GateUnavailable`) is not here: a state a test sets is
used as it is.
"""

import logging
import random
import threading
import time
from collections.abc import Callable
from datetime import datetime

from catcher.modules.youtube.gate_rules import (
    OPEN,
    GateState,
    Wait,
    after_block,
    after_reserve,
    after_success,
    block_length_hours,
    wait_for,
)

log = logging.getLogger("catcher.youtube")


class InMemoryGate:
    def __init__(
        self,
        *,
        min_gap_s: float = 120.0,
        jitter_s: float = 300.0,
        block_hours: float = 6.0,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.min_gap_s = max(0.0, min_gap_s)
        self.jitter_s = max(0.0, jitter_s)
        self.block_hours = max(0.0, block_hours)
        self.clock = clock
        self.rng = rng
        self.state: GateState = OPEN
        self._lock = threading.Lock()

    def peek(self) -> Wait | None:
        """Is a call allowed now? Changes nothing."""
        with self._lock:
            return wait_for(self.state, self.clock())

    def reserve(self) -> Wait | None:
        """Ask for a call. None means go ahead, and the gap to the next call has started."""
        with self._lock:
            now = self.clock()
            wait = wait_for(self.state, now)
            if wait is not None:
                return wait
            self.state = after_reserve(
                self.state, now, min_gap_s=self.min_gap_s, jitter_s=self.jitter_s, rng=self.rng
            )
            return None

    def record_success(self, started_at: float | None = None) -> None:
        """A fetch worked: close the breaker, unless a block was recorded after `started_at`."""
        with self._lock:
            state = self.state
            new = after_success(state, started_at)
            if new is state:
                return
            if state.streak or state.blocked_until:
                log.info("YouTube answered again: the breaker is closed")
            self.state = new

    def record_block(self, started_at: float | None = None) -> float:
        """YouTube said no. Open the breaker (6 hours, then 12, then 24) and return when it ends; a block
        recorded after this fetch started stays as it is."""
        with self._lock:
            now = self.clock()
            state = self.state
            new = after_block(state, now, started_at, block_hours=self.block_hours)
            if new is state:
                return state.blocked_until
            self.state = new
            log.error(
                "YouTube is blocking us (block %d): no calls for %g hours, until %s",
                new.streak,
                block_length_hours(new.streak, self.block_hours),
                datetime.fromtimestamp(new.blocked_until).strftime("%Y-%m-%d %H:%M"),
            )
            return new.blocked_until
