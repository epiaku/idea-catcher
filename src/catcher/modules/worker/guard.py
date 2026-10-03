"""One worker at a time: a session-level Postgres advisory lock held on a dedicated connection."""

import logging
from types import TracebackType
from typing import Self

from sqlalchemy import Connection, Engine, text

log = logging.getLogger("catcher.worker")

DEFAULT_KEY = 0x636174636865  # "catche" in ASCII


class WorkerAlreadyRunning(RuntimeError):
    """Another worker holds the lock."""


class WorkerLockLost(RuntimeError):
    """The lock connection died (a Postgres restart, a dropped connection): one worker is not guaranteed."""


LOST_MESSAGE = (
    "the worker lost its database lock (the connection to Postgres was lost or restarted); it stopped so "
    "that no second worker can run beside it: start it again"
)

# This backend is still the one that took the lock, and it still holds it. A session-level advisory lock
# on a bigint key shows in pg_locks as classid (high 32 bits), objid (low 32 bits) and objsubid 1.
_HELD = text(
    "select pg_backend_pid() = :pid and exists (select 1 from pg_locks where locktype = 'advisory' "
    "and granted and pid = pg_backend_pid() and classid::bigint = :hi and objid::bigint = :lo "
    "and objsubid = 1)"
)


class WorkerLock:
    """Holds `pg_try_advisory_lock(key)` for the lifetime of the `with` block.

    The lock belongs to the connection, so it also disappears when the process or the connection dies.
    `check()` (between jobs) tells the worker when that happened."""

    def __init__(self, engine: Engine, key: int = DEFAULT_KEY) -> None:
        self._engine = engine
        self._key = key
        self._connection: Connection | None = None
        self.backend_pid: int | None = None
        self._lost = False

    def __enter__(self) -> Self:
        # AUTOCOMMIT: no open transaction is left behind on the long-lived connection.
        connection = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        locked = False
        try:
            locked = bool(
                connection.execute(text("select pg_try_advisory_lock(:key)"), {"key": self._key}).scalar()
            )
            if not locked:
                connection.close()
                raise WorkerAlreadyRunning("another worker is already running; run one worker at a time")
            pid = connection.execute(text("select pg_backend_pid()")).scalar()
        except BaseException:
            _discard(connection, locked)
            raise
        self._connection = connection
        self.backend_pid = pid
        return self

    def check(self) -> None:
        """Raise WorkerLockLost unless this lock's own connection is alive, is still the backend that took
        the lock, and still holds it. One cheap query on the dedicated connection. Once lost, always lost: a
        connection that reconnected on its own would be a new backend without the lock."""
        connection = self._connection
        if self._lost or connection is None:
            raise WorkerLockLost(LOST_MESSAGE)
        params = {"pid": self.backend_pid, "hi": (self._key >> 32) & 0xFFFFFFFF, "lo": self._key & 0xFFFFFFFF}
        try:
            held = bool(connection.execute(_HELD, params).scalar())
        except Exception as error:
            log.error("the worker lock connection failed: %s", type(error).__name__)
            held = False
        if not held:
            self._lost = True
            raise WorkerLockLost(LOST_MESSAGE)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        released = False
        try:
            released = bool(
                connection.execute(text("select pg_advisory_unlock(:key)"), {"key": self._key}).scalar()
            )
            if not released:
                log.warning("the worker lock was not held at release; dropping its connection")
        except Exception as error:  # a dead connection already lost the lock
            log.warning("could not release the worker lock cleanly: %s", error)
        _discard(connection, locked=not released)


def _discard(connection: Connection, locked: bool) -> None:
    """Close the connection; when it may still hold the lock, drop the backend instead of repooling it,
    because a pooled session keeps its session-level advisory locks."""
    try:
        if locked:
            connection.invalidate()
        connection.close()
    except Exception as error:
        log.warning("could not close the worker lock connection: %s", error)
