"""The worker loop: claim one job at a time, run it, and reap the jobs other workers abandoned."""

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Literal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from catcher.core.db import session_scope
from catcher.modules.queue import queue
from catcher.modules.queue.items import ACTIVE_STATUSES, get_item, set_item_status
from catcher.modules.queue.models import Job
from catcher.modules.worker.dispatch import Outcome, run_job
from catcher.modules.worker.handlers import Handler, HandlerContext

log = logging.getLogger("catcher.worker")

RunOnceResult = Outcome | Literal["error"] | None
MAX_CLAIM_BACKOFF_S = 60.0


def fail_items_of(session: Session, jobs: Sequence[Job], *, now: datetime) -> list[str]:
    """The item of each job the reaper failed (a poison job that kept killing its worker) is `failed` too,
    with the job's error, when it is still active: no other job will move it. A job without a
    `calculated_name` param (`pipeline.run`) has no item; an item already in a final status (the job made
    its outcome, then died before it completed) is left alone. Returns the names it failed. Does not
    commit: it belongs in the reaper's transaction."""
    failed: list[str] = []
    for job in jobs:
        name = (job.params or {}).get("calculated_name")
        if job.status != "failed" or not isinstance(name, str):
            continue
        item = get_item(session, name)
        if item is None or item.status not in ACTIVE_STATUSES:
            continue
        set_item_status(session, name, "failed", now=now, reason=f"{job.type}: {job.error}")
        log.error("item %s: failed, its %s job %s: %s", name, job.type, job.id, job.error)
        failed.append(name)
    return failed


class Worker:
    """One worker, one job at a time. The clock for every queue call comes from `ctx.clock`; the timer
    that decides when to reap again is the monotonic clock of this process.

    Reaping happens between jobs only: while one long handler runs, nothing is reaped. When `run_job`
    raises ("error"), its heartbeat has stopped and the job stays `running` until its lease expires
    (`lease_s`, 120 s by default); then a reap requeues it.

    `sleep` is the idle wait; by default it is `stop.wait`, so an idle worker stops at once."""

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
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self.ctx = ctx
        self.handlers = handlers
        self.worker_id = worker_id
        self.lease_s = lease_s
        self.heartbeat_s = heartbeat_s
        self.poll_s = poll_s
        self.reap_every_s = reap_every_s
        self._sleep = sleep
        self._claim_failures = 0  # claims in a row that raised a database error

    def idle_wait_s(self) -> float:
        """How long to wait before the next claim: `poll_s`, doubled for each claim in a row that hit a
        database error, up to `MAX_CLAIM_BACKOFF_S` (or `poll_s` when that is longer)."""
        if self._claim_failures == 0:
            return self.poll_s
        cap = max(MAX_CLAIM_BACKOFF_S, self.poll_s)
        return min(self.poll_s * 2 ** min(self._claim_failures - 1, 32), cap)

    def run_once(self) -> RunOnceResult:
        """Claim the most urgent due job (in its own committed session) and run it. Returns the outcome
        of `run_job`, "error" when `run_job` raised (logged; the job stays running until the reaper
        requeues it), or None when no job is due or the claim hit a database error (logged; the next
        idle wait backs off). Any other error from the claim (a bad `lease_s`) propagates, as do
        KeyboardInterrupt and SystemExit."""
        try:
            with session_scope(self.ctx.engine) as session:
                job = queue.claim(session, worker=self.worker_id, now=self.ctx.clock(), lease_s=self.lease_s)
        except SQLAlchemyError as error:
            self._claim_failures += 1
            if self._claim_failures == 1:
                log.exception("worker %s could not claim a job; backing off", self.worker_id)
            else:
                log.warning(
                    "worker %s could not claim a job (%d in a row): %s",
                    self.worker_id,
                    self._claim_failures,
                    type(error).__name__,
                )
            return None
        if self._claim_failures:
            log.info("worker %s recovered after %d failed claim(s)", self.worker_id, self._claim_failures)
            self._claim_failures = 0
        if job is None:
            return None
        # The Job that claim returned carries the claim token (expire_on_commit=False keeps it loaded).
        try:
            return run_job(self.ctx, job, self.handlers, lease_s=self.lease_s, heartbeat_s=self.heartbeat_s)
        except Exception:
            log.exception("job %s (%s) could not be finished; the reaper will requeue it", job.id, job.type)
            return "error"

    def reap_safely(self) -> int:
        """Requeue (or fail) every job whose lease expired, and fail the item of a job it failed
        (`fail_items_of`), in one commit. Returns how many jobs changed; an error is logged and gives 0, so
        the reaper never stops the worker."""
        try:
            with session_scope(self.ctx.engine) as session:
                now = self.ctx.clock()
                changed = queue.reap(session, now=now)
                fail_items_of(session, changed, now=now)
        except Exception:
            log.exception("the reaper failed; it tries again later")
            return 0
        if changed:
            log.info("the reaper recovered %d job(s)", len(changed))
        return len(changed)

    def run_forever(self, stop: threading.Event) -> None:
        """Reap, then run jobs until `stop` is set; wait `idle_wait_s()` when nothing was run and reap
        again every `reap_every_s` (checked between jobs). A job that is running when `stop` is set
        finishes first; a stop that arrives just before a claim can let one more job start (it then
        finishes too)."""
        wait = self._sleep if self._sleep is not None else stop.wait
        self.reap_safely()
        next_reap = time.monotonic() + self.reap_every_s
        while not stop.is_set():
            if time.monotonic() >= next_reap:
                self.reap_safely()
                next_reap = time.monotonic() + self.reap_every_s
            if self.run_once() is None and not stop.is_set():
                wait(self.idle_wait_s())
