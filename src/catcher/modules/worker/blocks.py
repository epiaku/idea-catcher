"""The worker's memory of LLM backends not to call for a while.

An `llm.reason` job whose backend hit a usage limit, its budget, or was down (connection refused, a timeout)
blocks that backend until `now + LLM_BLOCK_S`. The jobs after it are deferred without a call; once the
block ends, the next document tries the backend once, and a new failure blocks it again.

The memory is per worker process and in memory only: a restart forgets it. One worker runs at a time, so
that is enough for now; B5 moves per-backend blocking into the Postgres `resources` table."""

import threading
from dataclasses import dataclass
from datetime import datetime

from catcher.core.db import require_aware


@dataclass(frozen=True)
class Block:
    until: datetime
    cause: str  # what the failed call said, for the reason of the documents it defers


class BackendBlocks:
    """Backend name -> `Block`, safe to use from several threads. Every time must be aware."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blocks: dict[str, Block] = {}

    def block(self, backend: str, until: datetime, cause: str) -> None:
        """Do not call `backend` before `until` (replaces an earlier block of it)."""
        require_aware(until)
        with self._lock:
            self._blocks[backend] = Block(until, cause)

    def entries(self, now: datetime) -> dict[str, Block]:
        """The blocks still running at `now`; the ended ones are dropped."""
        require_aware(now)
        with self._lock:
            for backend in [b for b, block in self._blocks.items() if block.until <= now]:
                del self._blocks[backend]
            return dict(self._blocks)

    def active(self, now: datetime) -> frozenset[str]:
        """The backends blocked at `now`."""
        return frozenset(self.entries(now))
