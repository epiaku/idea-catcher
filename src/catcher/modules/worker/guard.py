"""One worker at a time: a session-level Postgres advisory lock held on a dedicated connection."""

import logging
from types import TracebackType
from typing import Self

from sqlalchemy import Connection, Engine, text

log = logging.getLogger("catcher.worker")

DEFAULT_KEY = 0x636174636865  # "catche" in ASCII


class WorkerAlreadyRunning(RuntimeError):
    """Another worker holds the lock."""


class WorkerLock:
    """Holds `pg_try_advisory_lock(key)` for the lifetime of the `with` block.

    The lock belongs to the connection, so it also disappears when the process or the connection dies."""

    def __init__(self, engine: Engine, key: int = DEFAULT_KEY) -> None:
        self._engine = engine
        self._key = key
        self._connection: Connection | None = None
        self.backend_pid: int | None = None

    def __enter__(self) -> Self:
        # AUTOCOMMIT: no open transaction is left behind on the long-lived connection.
        connection = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            got = connection.execute(text("select pg_try_advisory_lock(:key)"), {"key": self._key}).scalar()
            pid = connection.execute(text("select pg_backend_pid()")).scalar()
        except BaseException:
            connection.close()
            raise
        if not got:
            connection.close()
            raise WorkerAlreadyRunning("another worker is already running; run one worker at a time")
        self._connection = connection
        self.backend_pid = pid
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            connection.execute(text("select pg_advisory_unlock(:key)"), {"key": self._key})
        except Exception as error:  # a dead connection already lost the lock
            log.warning("could not release the worker lock cleanly: %s", error)
        try:
            connection.close()
        except Exception as error:
            log.warning("could not close the worker lock connection: %s", error)
