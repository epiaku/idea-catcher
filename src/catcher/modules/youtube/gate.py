"""The gap and the breaker for calls to YouTube: what a gate is, and how a block looks.

A ban is per IP address and about how fast the requests come, and retrying during a block makes it longer.
So every fetch first asks the gate:

- **the gap:** at least `min_gap_s` (plus a random `jitter_s`) between the *start* of two fetches;
- **the breaker:** after a block (HTTP 429, a bot check) no call is made for `block_hours`, then twice as
  long, up to 24 hours, until a fetch works again.

The one gate is `PostgresGate` (`pg_gate.py`): its state is the `resources` row `youtube`, shared by every
command and every worker on the database. The rules themselves are in `gate_rules.py`.
"""

from typing import Protocol

from catcher.modules.youtube.gate_rules import MAX_BLOCK_HOURS, Wait

__all__ = ["MAX_BLOCK_HOURS", "Gate", "GateUnavailable", "Wait", "is_block_error"]

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
    CLI can handle it without importing SQLAlchemy."""


class Gate(Protocol):
    """What `YoutubeAccess` needs from a gate: `PostgresGate` (and `InMemoryGate` in the tests)."""

    def peek(self) -> Wait | None: ...

    def reserve(self) -> Wait | None: ...

    def record_success(self, started_at: float | None = None) -> None: ...

    def record_block(self, started_at: float | None = None) -> float: ...
