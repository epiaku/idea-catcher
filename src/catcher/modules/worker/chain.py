"""The chain: after a job ends, which job comes next. Only `ideas.pull` is scheduled; the rest follows.

    ideas.pull -> pipeline.run -> (youtube.fetch / llm.reason per document) -> pipeline.publish
    -> pipeline.run -> ...

The cycle repeats while files are left in the inbox. This module holds the pure rule (`decide`): the job that
ended, how it ended and a few `Facts` in, the job to queue (or nothing) out. No database, no files.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from catcher.core.db import session_scope
from catcher.core.git import GitError, ahead_of_upstream, has_remote
from catcher.modules.pipeline.inbox import scan_inbox
from catcher.modules.queue.items import items_in_status
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.queue.queue import count_runnable, enqueue, live_job_carries
from catcher.modules.worker.handlers import HandlerContext
from catcher.modules.worker.handlers_pipeline import _RECOVERABLE_ACTIVE, BACKFILL_ORIGIN

log = logging.getLogger("catcher.chain")

RUN = "pipeline.run"
PUBLISH = "pipeline.publish"
PULL = "ideas.pull"
DOCUMENT_JOBS = ("youtube.fetch", "llm.reason")

RUN_KEY = "chain:run"
PUBLISH_KEY = "chain:publish"


@dataclass(frozen=True)
class Facts:
    """What the rule needs to know, read from the database and the inbox after the job ended."""

    inbox_empty: bool  # nothing the scan of `pipeline.run` would stage
    deferred_due: bool  # a `deferred` or `stuck` item is due for another try
    unpublished_work: bool  # finished work that no publish has committed and pushed yet
    document_jobs_runnable: int  # queued or running `youtube.fetch`/`llm.reason` jobs due now (not delayed)
    run_active: bool  # a `pipeline.run` is queued or running
    latest_run_limited: bool  # the latest `pipeline.run` had `limit`, `only` or `requeue`
    last_run_made_progress: bool  # the latest `pipeline.run` took at least one document (true if none yet)
    chain_publish_push: bool  # `CHAIN_PUBLISH_PUSH`: the `pull` and `push` of a chained publish


@dataclass(frozen=True)
class Next:
    """The job to queue: its type, params and the dedupe key that keeps one of them active."""

    type: str
    params: dict[str, Any]
    dedupe_key: str


def _run(*, retry_deferred: bool) -> Next:
    return Next(RUN, {"retry_deferred": retry_deferred}, RUN_KEY)


def _publish(facts: Facts, *, follow_up: bool) -> Next:
    push = facts.chain_publish_push
    return Next(PUBLISH, {"pull": push, "push": push, "follow_up": follow_up}, PUBLISH_KEY)


def decide(job_type: str, outcome: str, params: dict[str, Any], facts: Facts) -> Next | None:
    """The job to queue after `job_type` ended with `outcome` ("succeeded", "failed", "deferred", "lost"),
    or None. `params` are the params of the job that ended.

    - `ideas.pull` succeeded: a run (that also retries the deferred) when files wait or a deferred item is
      due, else a publish when finished work is unpublished, else nothing. A pull that failed stops the
      cycle: the next scheduled pull tries again.
    - the run or a document job ended: a publish, but only when no document job can run now and no run is
      active. A delayed job (the YouTube gate, a retry delay) does not hold it back: when it ends later,
      this rule runs again. A failed run stops the chain (it could not read the inbox: more jobs would fail
      the same way). A preview is its own job type (`pipeline.preview`) and queues nothing.
    - `pipeline.publish` ended: a run (without retrying the deferred) when files are left, unless the publish
      says `follow_up` false (it follows a run with `limit`, `only` or `requeue`: at most one run) or the
      last run took no document at all (files the scan keeps leaving would loop for ever).
    """
    if outcome == "lost":
        return None
    if job_type == PULL:
        if outcome != "succeeded":
            return None
        if not facts.inbox_empty or facts.deferred_due:
            return _run(retry_deferred=True)
        if facts.unpublished_work:
            return _publish(facts, follow_up=True)
        return None
    if job_type == RUN or job_type in DOCUMENT_JOBS:
        if job_type == RUN and outcome == "failed":
            return None
        if facts.document_jobs_runnable or facts.run_active:
            return None
        return _publish(facts, follow_up=not facts.latest_run_limited)
    if job_type == PUBLISH:
        if outcome == "deferred" or not params.get("follow_up", True):
            return None
        if facts.inbox_empty or facts.run_active or not facts.last_run_made_progress:
            return None
        return _run(retry_deferred=False)
    return None


# --- the facts, and the job that is queued ---------------------------------------------------------------

_PROGRESS_COUNTS = (
    "staged",
    "adopted",
    "duplicates",
    "artifacts",
    "unreadable",
)  # each takes a file out of inbox/


def _limited(params: dict[str, Any]) -> bool:
    """True when a run's params leave files in the inbox on purpose: `limit` (also 0), `only` or `requeue`."""
    return params.get("limit") is not None or bool(params.get("only")) or bool(params.get("requeue"))


def _made_progress(result: dict[str, Any] | None) -> bool:
    return any(int((result or {}).get(key) or 0) for key in _PROGRESS_COUNTS)


def _git_ahead(ctx: HandlerContext) -> bool:
    """True when a repo with a remote has commits its upstream lacks (a push that failed earlier). A git
    error counts as no: the check only decides whether to start a publish, which will see the error itself."""
    for repo in (ctx.ideas, ctx.docs):
        try:
            if has_remote(repo, unattended=True) and ahead_of_upstream(repo, unattended=True):
                return True
        except GitError:
            continue
    return False


def read_facts(ctx: HandlerContext) -> Facts:
    """The facts `decide` needs, as they are now. Reads the inbox with the scan `pipeline.run` uses, so
    "empty" means: nothing the next run would take."""
    now = ctx.clock()
    scan = scan_inbox(ctx.ideas, now=now)
    inbox_empty = not (scan.notes or scan.artifacts or scan.errors)
    with session_scope(ctx.engine) as session:
        retryable = [i for i in items_in_status(session, "deferred", "stuck") if i.origin != BACKFILL_ORIGIN]
        orphaned = [
            i
            for i in items_in_status(session, *_RECOVERABLE_ACTIVE)
            if not live_job_carries(session, i.calculated_name)
        ]
        runs = list(
            session.scalars(
                select(Job).where(Job.type == RUN).order_by(Job.created_at.desc(), Job.id).limit(20)
            )
        )
        finished = next((job for job in runs if job.status == "succeeded"), None)
        last_publish = session.scalar(
            select(func.max(Job.finished_at)).where(Job.type == PUBLISH, Job.status == "succeeded")
        )
        newer = select(func.count()).select_from(JobItem).where(JobItem.status == "published")
        if last_publish is not None:
            newer = newer.where(JobItem.updated_at > last_publish)
        unpublished = bool(session.scalar(newer))
        return Facts(
            inbox_empty=inbox_empty,
            deferred_due=bool(retryable or orphaned),
            unpublished_work=unpublished or _git_ahead(ctx),
            document_jobs_runnable=count_runnable(session, now=now, types=DOCUMENT_JOBS),
            run_active=bool(
                session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.type == RUN, Job.status.in_(("queued", "running")))
                )
            ),
            latest_run_limited=bool(runs) and _limited(dict(runs[0].params or {})),
            last_run_made_progress=finished is None or _made_progress(finished.result),
            chain_publish_push=ctx.settings.chain_publish_push,
        )


def queue_next(ctx: HandlerContext, job: Job, outcome: str) -> Next | None:
    """After `job` ended with `outcome`: read the facts, ask `decide`, and queue what it says (one commit).
    Returns what was asked for (a job with that dedupe key may already have been active), or None."""
    chosen = decide(job.type, outcome, dict(job.params or {}), read_facts(ctx))
    if chosen is None:
        return None
    with session_scope(ctx.engine) as session:
        _, created = enqueue(
            session,
            type=chosen.type,
            now=ctx.clock(),
            params=chosen.params,
            dedupe_key=chosen.dedupe_key,
        )
    log.info(
        "chain: %s %s -> %s %s%s",
        job.type,
        outcome,
        chosen.type,
        chosen.params,
        "" if created else " (already active)",
    )
    return chosen
