"""The gap and the breaker for calls to YouTube.

A ban is per IP address and about how fast the requests come, and retrying during a block makes it longer.
So every fetch first asks this gate:

- **the gap:** at least `min_gap_s` (plus a random `jitter_s`) between the *start* of two fetches;
- **the breaker:** after a block (HTTP 429, a bot check) no call is made for `block_hours`, then twice as
  long, up to 24 hours, until a fetch works again.

The state is a small file on this machine (a ban belongs to this IP, so it is not kept in git). It is shared
by every run and every process on the machine, with a file lock around the check-and-reserve.

A state file that cannot be read is not trusted: it is kept as `youtube-gate.corrupt` and the gate **closes**
for `block_hours`, because losing a 24 hour block by accident is the expensive mistake.
"""

import json
import logging
import math
import os
import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Protocol

from catcher.core.files import file_lock, write_atomic
from catcher.modules.youtube.gate_rules import (
    MAX_BLOCK_HOURS,
    OPEN,
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

__all__ = ["MAX_BLOCK_HOURS", "Gate", "GateUnavailable", "Wait", "YoutubeGate", "is_block_error"]

log = logging.getLogger("catcher.youtube")

_STATE_KEYS = ("next_allowed_at", "blocked_until", "blocked_at", "streak")
_BOT_CHECKS = (
    "too many requests",
    "http error 429",
    "confirm you’re not a bot",
    "confirm you're not a bot",
    "ipblocked",
    "requestblocked",
    "blocking requests from your ip",
)


def is_block_error(error: BaseException) -> bool:
    """True when YouTube refused us for being too fast or too suspicious (a 429, a bot check).

    Any other failure is an ordinary failure and must not open the breaker. The HTTP status is looked at
    first, then the wording of the bot check, which comes without a status.
    """
    seen: set[int] = set()
    stack: list[BaseException | None] = [error]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "status", None) == 429 or getattr(current, "code", None) == 429:
            return True
        text = str(current).lower()
        if any(phrase in text for phrase in _BOT_CHECKS):
            return True
        stack.extend([current.__cause__, current.__context__])
        for part in getattr(current, "exc_info", None) or ():  # yt-dlp keeps its cause here
            if isinstance(part, BaseException):
                stack.append(part)
    return False


class GateUnavailable(RuntimeError):
    """The gate could not read or write its state (the database is down, a lock timed out): do not fetch.

    Raised by the Postgres gate (`pg_gate.py`); kept here, next to the protocol, so `YoutubeAccess` and the
    Stage A CLI can handle it without importing SQLAlchemy."""


class Gate(Protocol):
    """What `YoutubeAccess` needs from a gate: the file gate here, a database gate later."""

    def peek(self) -> Wait | None: ...

    def reserve(self) -> Wait | None: ...

    def record_success(self, started_at: float | None = None) -> None: ...

    def record_block(self, started_at: float | None = None) -> float: ...


class YoutubeGate:
    def __init__(
        self,
        state_dir: Path,
        *,
        min_gap_s: float = 120.0,
        jitter_s: float = 300.0,
        block_hours: float = 6.0,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.state_file = state_dir.expanduser() / "youtube-gate.json"
        self.min_gap_s = max(0.0, min_gap_s)
        self.jitter_s = max(0.0, jitter_s)
        self.block_hours = max(0.0, block_hours)
        self.clock = clock
        self.rng = rng

    # ---- state on disk ----------------------------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with file_lock(self.state_file.with_suffix(".lock")):
            yield

    def _read(self, now: float) -> GateState:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            values = {key: self._number(raw, key) for key in _STATE_KEYS}
            streak = values.pop("streak")  # a float in the file, an int in the rules
            return clamp(GateState(**values, streak=int(streak)), now)
        except FileNotFoundError:
            return OPEN
        except (ValueError, OSError, AttributeError, TypeError) as e:
            return self._fail_closed(now, e)

    @staticmethod
    def _number(raw: dict[str, object], key: str) -> float:
        value = float(raw.get(key, 0))  # type: ignore[arg-type]
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} is {value!r}")
        return value

    def _fail_closed(self, now: float, error: Exception) -> GateState:
        """The state file is damaged: keep it for a look, and treat YouTube as blocked for `block_hours`."""
        kept = self.state_file.with_suffix(".corrupt")
        with suppress(OSError):
            os.replace(self.state_file, kept)
        state = closed_state(now, self.block_hours)
        log.error(
            "the YouTube gate file %s is unreadable (%s): kept as %s, and no calls for %g hours to be safe",
            self.state_file,
            error,
            kept.name,
            max(self.block_hours, 1.0),
        )
        self._write(state)
        return state

    def _write(self, state: GateState) -> None:
        data = {
            "next_allowed_at": state.next_allowed_at,
            "blocked_until": state.blocked_until,
            "blocked_at": state.blocked_at,
            "streak": float(state.streak),  # a float, as the file has always had it
        }
        write_atomic(self.state_file, json.dumps(data))  # never a half-written file

    # ---- the gate ---------------------------------------------------------------------------------

    def peek(self) -> Wait | None:
        """Is a call allowed now? Changes nothing (apart from closing the gate over a damaged file)."""
        with self._locked():
            now = self.clock()
            return wait_for(self._read(now), now)

    def reserve(self) -> Wait | None:
        """Ask for a call. None means go ahead, and the gap to the next call has started."""
        with self._locked():
            now = self.clock()
            state = self._read(now)
            wait = wait_for(state, now)
            if wait is not None:
                return wait
            self._write(
                after_reserve(state, now, min_gap_s=self.min_gap_s, jitter_s=self.jitter_s, rng=self.rng)
            )
            return None

    def record_success(self, started_at: float | None = None) -> None:
        """A fetch worked: close the breaker. `started_at` is when that fetch was reserved: a block recorded
        after it (by another process) is newer news and stays."""
        with self._locked():
            state = self._read(self.clock())
            new = after_success(state, started_at)
            if new is state:
                return
            if state.streak or state.blocked_until:
                log.info("YouTube answered again: the breaker is closed")
            self._write(new)

    def record_block(self, started_at: float | None = None) -> float:
        """YouTube said no. Open the breaker (6 hours, then 12, then 24) and return when it ends.

        Two fetches that were running together and both get the no are one block, not two: when a block was
        recorded after this fetch started, the breaker stays as it is."""
        with self._locked():
            now = self.clock()
            state = self._read(now)
            new = after_block(state, now, started_at, block_hours=self.block_hours)
            if new is state:
                return state.blocked_until
            self._write(new)
            log.error(
                "YouTube is blocking us (block %d): no calls for %g hours, until %s",
                new.streak,
                block_length_hours(new.streak, self.block_hours),
                datetime.fromtimestamp(new.blocked_until).strftime("%Y-%m-%d %H:%M"),
            )
            return new.blocked_until
