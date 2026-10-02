"""The gap and the breaker for calls to YouTube.

A ban is per IP address and about how fast the requests come, and retrying during a block makes it longer.
So every fetch first asks this gate:

- **the gap:** at least `min_gap_s` (plus a random `jitter_s`) between the *start* of two fetches;
- **the breaker:** after a block (HTTP 429, a bot check) no call is made for `block_hours`, then twice as
  long, up to 24 hours, until a fetch works again.

The state is a small file on this machine (a ban belongs to this IP, so it is not kept in git). It is shared
by every run and every process on the machine, with a file lock around the check-and-reserve.
"""

import fcntl
import json
import logging
import os
import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("catcher.youtube")

MAX_BLOCK_HOURS = 24.0
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


@dataclass(frozen=True)
class Wait:
    """Why a call to YouTube is not allowed yet, and until when (seconds since the epoch)."""

    until: float
    blocked: bool  # True: the breaker is open (hours). False: just the gap between two fetches

    def message(self, now: float | None = None) -> str:
        moment = datetime.fromtimestamp(self.until)
        today = datetime.fromtimestamp(now if now is not None else time.time()).date()
        clock = moment.strftime("%H:%M") if moment.date() == today else moment.strftime("%Y-%m-%d %H:%M")
        return f"YouTube blocked until {clock}" if self.blocked else f"YouTube: next call allowed at {clock}"


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
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict[str, float]:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            return {
                "next_allowed_at": float(raw.get("next_allowed_at", 0)),
                "blocked_until": float(raw.get("blocked_until", 0)),
                "streak": float(raw.get("streak", 0)),
            }
        except FileNotFoundError:
            pass
        except (ValueError, OSError, AttributeError) as e:
            log.warning("ignoring an unreadable %s: %s", self.state_file, e)
        return {"next_allowed_at": 0.0, "blocked_until": 0.0, "streak": 0.0}

    def _write(self, state: dict[str, float]) -> None:
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temp, self.state_file)  # never a half-written file

    @staticmethod
    def _wait(state: dict[str, float], now: float) -> Wait | None:
        if now < state["blocked_until"]:
            return Wait(state["blocked_until"], blocked=True)
        if now < state["next_allowed_at"]:
            return Wait(state["next_allowed_at"], blocked=False)
        return None

    # ---- the gate ---------------------------------------------------------------------------------

    def peek(self) -> Wait | None:
        """Is a call allowed now? Changes nothing."""
        with self._locked():
            return self._wait(self._read(), self.clock())

    def reserve(self) -> Wait | None:
        """Ask for a call. None means go ahead, and the gap to the next call has started."""
        with self._locked():
            now = self.clock()
            state = self._read()
            wait = self._wait(state, now)
            if wait is not None:
                return wait
            state["next_allowed_at"] = now + self.min_gap_s + self.rng() * self.jitter_s
            self._write(state)
            return None

    def record_success(self) -> None:
        with self._locked():
            state = self._read()
            if state["streak"] or state["blocked_until"]:
                log.info("YouTube answered again: the breaker is closed")
            state["streak"], state["blocked_until"] = 0.0, 0.0
            self._write(state)

    def record_block(self) -> float:
        """YouTube said no. Open the breaker (6 hours, then 12, then 24) and return when it ends."""
        with self._locked():
            now = self.clock()
            state = self._read()
            state["streak"] += 1
            hours = min(self.block_hours * 2 ** (state["streak"] - 1), max(MAX_BLOCK_HOURS, self.block_hours))
            state["blocked_until"] = now + hours * 3600
            self._write(state)
            log.error(
                "YouTube is blocking us (block %d): no calls for %g hours, until %s",
                int(state["streak"]),
                hours,
                datetime.fromtimestamp(state["blocked_until"]).strftime("%Y-%m-%d %H:%M"),
            )
            return state["blocked_until"]
