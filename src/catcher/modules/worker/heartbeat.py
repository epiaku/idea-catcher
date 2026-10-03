"""A thread that keeps a running job's lease alive while its handler works."""

import logging
import math
import threading
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Self

from sqlalchemy import Engine, inspect
from sqlalchemy.orm.attributes import set_committed_value

from catcher.core.db import session_scope
from catcher.modules.queue import queue
from catcher.modules.queue.models import Job

log = logging.getLogger("catcher.worker")

_TOKEN = ("id", "status", "locked_by", "claim_seq")
_JOIN_S = 15.0  # a beat stuck in the database (lock_timeout is 10 s) still lets the worker move on


def _check_positive(name: str, value: float) -> None:
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a finite number above 0, not {value!r}")


def _snapshot(job: Job) -> Job:
    """A separate Job carrying only the claim token, as committed state (no unsaved changes), so the
    thread never reads or writes the Job object the handler is using."""
    loaded = inspect(job).dict
    missing = [key for key in _TOKEN if key not in loaded]
    if missing:
        raise ValueError(f"the job's claim token is not loaded ({', '.join(missing)}); pass the claimed Job")
    copy = Job()
    for key in _TOKEN:
        set_committed_value(copy, key, loaded[key])
    return copy


class Heartbeat:
    """Context manager: while it is open a daemon thread extends the job's lease every `interval_s`.

    Each beat opens its own session and fences on its own copy of the claim token. A beat that finds
    the job is no longer ours sets `lost` and ends the thread; a beat that raises is logged and the
    thread tries again at the next interval."""

    def __init__(
        self,
        engine: Engine,
        job: Job,
        *,
        lease_s: float,
        interval_s: float,
        clock: Callable[[], datetime],
    ) -> None:
        _check_positive("lease_s", lease_s)
        _check_positive("interval_s", interval_s)
        self._engine = engine
        self._token = _snapshot(job)
        self._job_id = self._token.id
        self._lease_s = lease_s
        self._interval_s = interval_s
        self._clock = clock
        self._stop = threading.Event()
        self._lost = False
        self.thread = threading.Thread(target=self._run, name=f"heartbeat-{self._job_id}", daemon=True)

    @property
    def lost(self) -> bool:
        """True once a beat found that the job is no longer ours."""
        return self._lost

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self.thread.join(_JOIN_S)
        if self.thread.is_alive():
            log.warning("heartbeat of job %s did not stop within %s s", self._job_id, _JOIN_S)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                with session_scope(self._engine) as session:
                    kept = queue.heartbeat(session, self._token, now=self._clock(), lease_s=self._lease_s)
            except Exception:
                log.warning("heartbeat of job %s failed; trying again", self._job_id, exc_info=True)
                continue
            if not kept:
                self._lost = True
                log.warning("job %s lost its lease; the heartbeat stops", self._job_id)
                return
