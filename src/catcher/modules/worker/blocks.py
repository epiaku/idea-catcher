"""The LLM backends (and profiles) not to call for a while, kept in the Postgres `resources` table.

An `llm.reason` job whose backend hit a usage limit or was down (connection refused, a timeout) blocks that
backend until `now + LLM_BLOCK_S`; a used-up budget blocks it for `LLM_BUDGET_BLOCK_S`; a model the backend
does not know blocks only the profile (`<backend>:<profile>`) for `LLM_BLOCK_S`. The jobs after it are
deferred without a call; once the block ends, the next document tries once, and a new failure blocks again.

One `resources` row per key: `openai`, `freellmapi`, `fake` for a backend, `openai:clippings` for a profile.
A MISSING row means OPEN (unlike `youtube`, where a missing row means closed): an LLM block is only ever
created by a failure. The rows survive a restart and are shared by every worker. Two workers blocking the
same key at once leave one row with the later end. No job carries an LLM key as its `resource`, so these rows
never hold back the claim of a job.

A Postgres error while reading never reads as open: every known backend counts as blocked for
`UNAVAILABLE_S` (one poll), and it is logged once. A Postgres error while blocking is logged and raises
nothing: the document is already deferred, and the next failure blocks again."""

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import get_args

from sqlalchemy import Engine, case, delete, or_, select
from sqlalchemy.dialects.postgresql import insert

from catcher.core.db import require_aware, session_scope
from catcher.modules.llm.profiles import BackendName
from catcher.modules.queue.models import Resource

log = logging.getLogger("catcher.worker.blocks")

UNAVAILABLE_S = 30  # how long a backend counts as blocked when the blocks cannot be read


@dataclass(frozen=True)
class Block:
    until: datetime
    cause: str  # what the failed call said, for the reason of the documents it defers


def every_backend() -> list[str]:
    """Every backend the profiles can name."""
    return list(get_args(BackendName))


def profile_key(backend: str, profile: str) -> str:
    """The key of a block of one profile of `backend` (a wrong model), not of the whole backend."""
    return f"{backend}:{profile}"


class BackendBlocks:
    """Key -> `Block` in the `resources` table on `engine`, safe to use from several threads.

    `clock` gives `blocked_at`; `known_backends` names the backends whose rows are LLM blocks (other rows,
    such as `youtube`, are not) and that count as blocked when Postgres cannot be read. Times are aware."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime],
        known_backends: Callable[[], Iterable[str]] = every_backend,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._known_backends = known_backends
        self._lock = threading.Lock()
        self._unreadable = False  # the last read failed: log the next failure no more, the recovery once

    def block(self, key: str, until: datetime, cause: str) -> None:
        """Do not call `key` (a backend, or `<backend>:<profile>`) before `until`. A running block that ends
        later is kept (with its cause). A Postgres error is logged, never raised."""
        require_aware(until)
        now = require_aware(self._clock())
        values = {
            "name": key,
            "blocked_until": until,
            "blocked_at": now,
            "reason": cause,
            "streak": 0,
            "concurrency": 1,
            "updated_at": now,
        }
        statement = insert(Resource).values(**values)
        new = statement.excluded
        later = or_(Resource.blocked_until.is_(None), new.blocked_until >= Resource.blocked_until)
        statement = statement.on_conflict_do_update(
            index_elements=[Resource.name],
            set_={
                "blocked_until": case((later, new.blocked_until), else_=Resource.blocked_until),
                "reason": case((later, new.reason), else_=Resource.reason),
                "blocked_at": new.blocked_at,
                "updated_at": new.updated_at,
            },
        )
        try:
            with session_scope(self._engine) as session:
                session.execute(statement)
        except Exception as e:
            log.error("cannot save the block of LLM %s until %s: %s", key, until.isoformat(), _short(e))

    def entries(self, now: datetime) -> dict[str, Block]:
        """The blocks still running at `now`. When Postgres cannot be read, every known backend is blocked
        for UNAVAILABLE_S (never open); this never raises."""
        require_aware(now)
        try:
            known = sorted(set(self._known_backends()))
        except Exception as e:  # the profiles cannot be read: no LLM call goes anywhere then either
            log.error("cannot list the LLM backends to check their blocks: %s", e)
            known = every_backend()
        try:
            with session_scope(self._engine) as session:
                rows = session.execute(
                    select(Resource.name, Resource.blocked_until, Resource.reason).where(
                        Resource.blocked_until > now,
                        or_(
                            Resource.name.in_(known),
                            *(Resource.name.startswith(f"{b}:", autoescape=True) for b in known),
                        ),
                    )
                ).all()
        except Exception as e:
            return self._unavailable(known, now, e)
        with self._lock:
            if self._unreadable:
                self._unreadable = False
                log.info("the LLM blocks can be read again")
        return {name: Block(until, reason or "") for name, until, reason in rows if until is not None}

    def active(self, now: datetime) -> frozenset[str]:
        """The keys blocked at `now`."""
        return frozenset(self.entries(now))

    def unblock(self, key: str) -> None:
        """Open `key` again (tests, or by hand): its row is removed, and a missing row is open."""
        with session_scope(self._engine) as session:
            session.execute(delete(Resource).where(Resource.name == key))

    def _unavailable(self, known: list[str], now: datetime, error: Exception) -> dict[str, Block]:
        cause = f"block memory unavailable: {_short(error)}"
        with self._lock:
            first = not self._unreadable
            self._unreadable = True
        if first:
            log.error(
                "cannot read the LLM blocks, so %s count as blocked until they can be read: %s",
                ", ".join(known) or "no backend",
                _short(error),
            )
        until = now + timedelta(seconds=UNAVAILABLE_S)
        return {backend: Block(until, cause) for backend in known}


def _short(error: Exception) -> str:
    """The first line of an error, at most 200 characters (a SQLAlchemy error carries the statement)."""
    text = str(error).strip().splitlines()
    return f"{type(error).__name__}: {text[0][:200] if text else ''}"
