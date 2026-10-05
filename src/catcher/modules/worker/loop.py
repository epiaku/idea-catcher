"""The worker loop: claim one job at a time, run it, and reap the jobs other workers abandoned."""

import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from catcher.core.config import Settings
from catcher.core.db import session_scope
from catcher.modules.pipeline.inbox import move_to_failed
from catcher.modules.queue import queue
from catcher.modules.queue.items import ACTIVE_STATUSES, get_item
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.queue.states import ItemStates, mark_stuck
from catcher.modules.worker.dispatch import Outcome, run_job
from catcher.modules.worker.handlers import Handler, HandlerContext

log = logging.getLogger("catcher.worker")

RunOnceResult = Outcome | Literal["error", "claim_error"] | None
MAX_CLAIM_BACKOFF_S = 60.0


@dataclass(frozen=True)
class FailedItem:
    """An item the reaper failed, with what its working copy needs to be filed in `failed/`, and its row
    (for the mirror, written once the file is there)."""

    name: str
    reason: str
    doc_id: str
    doc_class: str
    row: JobItem | None = field(default=None, compare=False, repr=False)


def _other_active_job(session: Session, job: Job, name: str) -> bool:
    """True when another queued or running job carries item `name`: the handler handed it on (fetch to
    reason, or back) before it died, and that job owns the item now."""
    return queue.live_job_carries(session, name, besides=job.id)


def fail_items_of(
    session: Session, jobs: Sequence[Job], *, now: datetime, states: ItemStates | None = None
) -> list[FailedItem]:
    """The item of each job the reaper failed (a poison job that kept killing its worker) is `failed` too,
    with the reason `<job type>: <job error>`, when nothing else will move it: it is still active and no
    other queued or running job carries it (after a hand-off the next job owns it). A job without a
    `calculated_name` param (`pipeline.run`) has no item; an item already in a final status (the job made
    its outcome, then died before it completed) is left alone. Each item is changed in its own savepoint:
    an error is logged, that item is left as it was and the reap goes on. The status changes through
    `states` (the one writer: an error event per item). Returns the items it failed. Does not commit: it
    belongs in the reaper's transaction."""
    states = states if states is not None else ItemStates()
    failed: list[FailedItem] = []
    for job in jobs:
        name = (job.params or {}).get("calculated_name")
        if job.status != "failed" or not isinstance(name, str):
            continue
        reason = f"{job.type}: {job.error}"
        try:
            with session.begin_nested():
                if _other_active_job(session, job, name):
                    log.info(
                        "item %s: left to its next job (the %s job %s was failed)", name, job.type, job.id
                    )
                    continue
                item = get_item(session, name)
                if item is None or item.status not in ACTIVE_STATUSES:
                    continue
                row = states.transition(
                    session, name, "failed", now=now, reason=reason, job_id=job.id, level="error"
                )
                failed.append(FailedItem(name, reason, item.doc_id, item.doc_class, row))
        except Exception:
            log.exception(
                "item %s: could not be failed after its %s job %s; left as it was", name, job.type, job.id
            )
            continue
        log.error("item %s: failed, its %s job %s: %s", name, job.type, job.id, job.error)
    return failed


def file_failed_items(
    ideas: Path, items: Sequence[FailedItem], *, now: datetime, states: ItemStates | None = None
) -> None:
    """Best effort, after the reaper's commit: move each failed item's working copy from `output/` to
    `failed/` with the reason, as Stage A does (`move_to_failed`), then mirror its state into that file
    (`states.mirror_after_commit`). A missing working copy is skipped; a file error is logged, never raised
    (the database already says `failed`, and a requeue starts from `archive/` either way)."""
    for item in items:
        out = ideas / "output" / item.name
        try:
            if out.is_file():
                move_to_failed(ideas, out, item.reason, doc_id=item.doc_id, doc_class=item.doc_class, now=now)
        except Exception as e:
            log.error("item %s: could not move output/%s to failed/: %s", item.name, item.name, e)
        if states is not None and item.row is not None:
            states.mirror_after_commit(item.row)


class Worker:
    """One worker, one job at a time. The clock for every queue call comes from `ctx.clock`; the timer
    that decides when to reap again is the monotonic clock of this process.

    Reaping happens between jobs only: while one long handler runs, nothing is reaped. When `run_job`
    raises ("error"), its heartbeat has stopped and the job stays `running` until its lease expires
    (`lease_s`, 120 s by default); then a reap requeues it.

    `sleep` is the idle wait; by default it is `stop.wait`, so an idle worker stops at once.

    Each reap also marks the items deferred for `stuck_after_days` days `stuck` (`mark_stuck`); by default
    the days come from `ctx.settings` (STUCK_AFTER_DAYS), and with no such setting nothing is marked."""

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
        lock_check: Callable[[], None] | None = None,
        stuck_after_days: float | None = None,
    ) -> None:
        self.ctx = ctx
        self.handlers = handlers
        self.worker_id = worker_id
        self.lease_s = lease_s
        self.heartbeat_s = heartbeat_s
        self.poll_s = poll_s
        self.reap_every_s = reap_every_s
        self._sleep = sleep
        self._lock_check = lock_check  # `WorkerLock.check`: raises WorkerLockLost when the lock is gone
        self._claim_failures = 0  # claims in a row that raised a database error
        if stuck_after_days is None and isinstance(ctx.settings, Settings):
            stuck_after_days = ctx.settings.stuck_after_days
        self.stuck_after_days = stuck_after_days

    def idle_wait_s(self) -> float:
        """How long to wait before the next claim: `poll_s`, doubled for each claim in a row that hit a
        database error, up to `MAX_CLAIM_BACKOFF_S` (or `poll_s` when that is longer)."""
        if self._claim_failures == 0:
            return self.poll_s
        cap = max(MAX_CLAIM_BACKOFF_S, self.poll_s)
        return min(self.poll_s * 2 ** min(self._claim_failures - 1, 32), cap)

    def run_once(self) -> RunOnceResult:
        """Claim the most urgent due job (in its own committed session) and run it. Returns the outcome
        of `run_job` (after a "failed" one, its item is failed too: `fail_item_of`), "error" when
        `run_job` raised (logged; the job stays running until the reaper requeues it), "claim_error" when
        the claim hit a database error (logged; the next idle wait backs off), or None when no job is
        due. Any other error from the claim (a bad `lease_s`) propagates, as do KeyboardInterrupt and
        SystemExit.

        Before the claim, `lock_check` (when given) makes sure this is still the one worker; its
        WorkerLockLost propagates and stops the worker."""
        self.check_lock()
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
            return "claim_error"
        if self._claim_failures:
            log.info("worker %s recovered after %d failed claim(s)", self.worker_id, self._claim_failures)
            self._claim_failures = 0
        if job is None:
            return None
        # The Job that claim returned carries the claim token (expire_on_commit=False keeps it loaded).
        try:
            outcome = run_job(
                self.ctx, job, self.handlers, lease_s=self.lease_s, heartbeat_s=self.heartbeat_s
            )
        except Exception:
            log.exception("job %s (%s) could not be finished; the reaper will requeue it", job.id, job.type)
            return "error"
        if outcome == "failed":
            self.fail_item_of(job.id)
        return outcome

    def fail_item_of(self, job_id: uuid.UUID) -> None:
        """After a job ended `failed` (a handler's `Fail`, or an exception `run_job` turned into one), its
        item must not stay active with nothing to move it: the reaper's `fail_items_of` fails it with the
        job's error (unless another queued or running job carries it, or it is already final), in one
        commit, and `file_failed_items` then moves its working copy to `failed/`. An error is logged, never
        raised: the job is already finished, and the worker goes on."""
        try:
            with session_scope(self.ctx.engine) as session:
                now = self.ctx.clock()
                job = session.get(Job, job_id)
                failed = fail_items_of(
                    session, [job] if job is not None else [], now=now, states=self.ctx.item_states
                )
        except Exception:
            log.exception("job %s failed, and could not fail the item it carries", job_id)
            return
        file_failed_items(self.ctx.ideas, failed, now=now, states=self.ctx.item_states)

    def check_lock(self) -> None:
        """Run `lock_check` when given: WorkerLockLost when this is no longer the one worker."""
        if self._lock_check is not None:
            self._lock_check()

    def reap_safely(self) -> int:
        """Requeue (or fail) every job whose lease expired, and fail the item of a job it failed
        (`fail_items_of`), in one commit; then move those items' working copies to `failed/`
        (`file_failed_items`). After a reap that worked, in a commit of its own, mark the long-deferred
        items `stuck` (`mark_stuck_safely`). Returns how many jobs changed; an error is logged and gives 0, so
        the reaper never stops the worker."""
        try:
            with session_scope(self.ctx.engine) as session:
                now = self.ctx.clock()
                changed = queue.reap(session, now=now)
                failed = fail_items_of(session, changed, now=now, states=self.ctx.item_states)
        except Exception:
            log.exception("the reaper failed; it tries again later")
            return 0
        # file IO after the commit, never in it: the move to failed/, then the mirror into that file
        file_failed_items(self.ctx.ideas, failed, now=now, states=self.ctx.item_states)
        if changed:
            log.info("the reaper recovered %d job(s)", len(changed))
        self.mark_stuck_safely()
        return len(changed)

    def mark_stuck_safely(self) -> int:
        """Mark the items deferred for `stuck_after_days` days `stuck` (`mark_stuck`), in one commit, then
        mirror them into their working copies. Returns how many; nothing when `stuck_after_days` is None.
        An error is logged and gives 0: it never stops the reaper or the worker."""
        if self.stuck_after_days is None:
            return 0
        try:
            with session_scope(self.ctx.engine) as session:
                stuck = mark_stuck(
                    session,
                    now=self.ctx.clock(),
                    after_days=self.stuck_after_days,
                    states=self.ctx.item_states,
                )
        except Exception:
            log.exception("marking the long-deferred items stuck failed; it tries again at the next reap")
            return 0
        for item in stuck:
            self.ctx.item_states.mirror_after_commit(item)
        return len(stuck)

    def run_forever(self, stop: threading.Event) -> None:
        """Reap, then run jobs until `stop` is set; wait `idle_wait_s()` when nothing was run and reap
        again every `reap_every_s` (checked between jobs). A job that is running when `stop` is set
        finishes first; a stop that arrives just before a claim can let one more job start (it then
        finishes too). WorkerLockLost from `check_lock` (before each reap and each claim) propagates."""
        wait = self._sleep if self._sleep is not None else stop.wait
        self.check_lock()
        self.reap_safely()
        next_reap = time.monotonic() + self.reap_every_s
        while not stop.is_set():
            if time.monotonic() >= next_reap:
                self.check_lock()
                self.reap_safely()
                next_reap = time.monotonic() + self.reap_every_s
            if self.run_once() in (None, "claim_error") and not stop.is_set():
                wait(self.idle_wait_s())
