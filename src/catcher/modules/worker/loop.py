"""The worker loop: claim one job at a time, run it, and reap the jobs other workers abandoned."""

import logging
import threading
import time
from collections.abc import Callable, Mapping

from catcher.core.db import session_scope
from catcher.modules.queue import queue
from catcher.modules.worker.dispatch import run_job
from catcher.modules.worker.handlers import Handler, HandlerContext

log = logging.getLogger("catcher.worker")


class Worker:
    """One worker, one job at a time. The clock for every queue call comes from `ctx.clock`; the timer
    that decides when to reap again is the monotonic clock of this process."""

    def __init__(
        self,
        ctx: HandlerContext,
        handlers: Mapping[str, Handler],
        *,
        worker_id: str,
        lease_s: float = 120.0,
        heartbeat_s: float = 30.0,
        poll_s: float = 2.0,
        reap_every_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ctx = ctx
        self.handlers = handlers
        self.worker_id = worker_id
        self.lease_s = lease_s
        self.heartbeat_s = heartbeat_s
        self.poll_s = poll_s
        self.reap_every_s = reap_every_s
        self._sleep = sleep

    def run_once(self) -> str | None:
        """Claim the most urgent due job (in its own committed session) and run it. Returns the outcome
        of `run_job` ("succeeded", "deferred", "failed", "lost"), "error" when `run_job` raised (logged;
        the job stays running until the reaper requeues it), or None when no job is due or the claim
        itself failed (logged). KeyboardInterrupt and SystemExit propagate."""
        try:
            with session_scope(self.ctx.engine) as session:
                job = queue.claim(session, worker=self.worker_id, now=self.ctx.clock(), lease_s=self.lease_s)
        except Exception:
            log.exception("worker %s could not claim a job", self.worker_id)
            return None
        if job is None:
            return None
        # The Job that claim returned carries the claim token (expire_on_commit=False keeps it loaded).
        try:
            return run_job(self.ctx, job, self.handlers, lease_s=self.lease_s, heartbeat_s=self.heartbeat_s)
        except Exception:
            log.exception("job %s (%s) could not be finished; the reaper will requeue it", job.id, job.type)
            return "error"

    def reap_safely(self) -> int:
        """Requeue (or fail) every job whose lease expired. Returns how many changed; an error is logged
        and gives 0, so the reaper never stops the worker."""
        try:
            with session_scope(self.ctx.engine) as session:
                changed = queue.reap(session, now=self.ctx.clock())
        except Exception:
            log.exception("the reaper failed; it tries again later")
            return 0
        if changed:
            log.info("the reaper recovered %d job(s)", len(changed))
        return len(changed)

    def run_forever(self, stop: threading.Event) -> None:
        """Reap, then run jobs until `stop` is set; sleep `poll_s` when nothing is due and reap again
        every `reap_every_s`. A job that is running when `stop` is set finishes first."""
        self.reap_safely()
        next_reap = time.monotonic() + self.reap_every_s
        while not stop.is_set():
            if time.monotonic() >= next_reap:
                self.reap_safely()
                next_reap = time.monotonic() + self.reap_every_s
            if self.run_once() is None and not stop.is_set():
                self._sleep(self.poll_s)
