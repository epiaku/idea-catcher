"""The chain: after a job ends, which job comes next. Only `ideas.pull` is scheduled; the rest follows.

    ideas.pull -> pipeline.run -> (youtube.fetch / llm.reason per document) -> pipeline.publish
    -> pipeline.run -> ...

The cycle repeats while files are left in the inbox. This module holds the pure rule (`decide`): the job that
ended, how it ended and a few `Facts` in, the job to queue (or nothing) out. No database, no files.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from catcher.core.db import session_scope
from catcher.core.git import GitError, ahead_of_upstream, has_remote
from catcher.modules.pipeline.inbox import scan_inbox
from catcher.modules.queue.items import items_in_status
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.queue.queue import count_runnable, enqueue, live_job_carries
from catcher.modules.worker.handlers import HandlerContext
from catcher.modules.worker.handlers_pipeline import _RECOVERABLE_ACTIVE, BACKFILL_ORIGIN, parse_pull_params

log = logging.getLogger("catcher.chain")

RUN = "pipeline.run"
PUBLISH = "pipeline.publish"
PULL = "ideas.pull"
DOCUMENT_JOBS = ("youtube.fetch", "llm.reason")
BACKFILL_JOBS = ("backfill.docs", "backfill.channel")  # they release clip notes into the inbox

PULL_KEY = "chain:pull"
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
    backfill_released: bool  # the backfill job that ended released at least one note (its stored result)


@dataclass(frozen=True)
class Next:
    """The job to queue: its type, params and the dedupe key that keeps one of them active."""

    type: str
    params: dict[str, Any]
    dedupe_key: str


# What `read_facts` fills in for a fact the job type does not need (`decide` does not look at it).
NEUTRAL: dict[str, Any] = {
    "inbox_empty": True,
    "deferred_due": False,
    "unpublished_work": False,
    "document_jobs_runnable": 0,
    "run_active": False,
    "latest_run_limited": False,
    "last_run_made_progress": True,
    "chain_publish_push": True,
    "backfill_released": False,
}


def _run(*, retry_deferred: bool, limit: int | None = None) -> Next:
    run_params: dict[str, Any] = {"retry_deferred": retry_deferred}
    if limit is not None:
        run_params["limit"] = limit
    return Next(RUN, run_params, RUN_KEY)


def _publish(facts: Facts, *, follow_up: bool) -> Next:
    push = facts.chain_publish_push
    return Next(PUBLISH, {"pull": push, "push": push, "follow_up": follow_up}, PUBLISH_KEY)


def decide(job_type: str, outcome: str, params: dict[str, Any], facts: Facts) -> Next | None:
    """The job to queue after `job_type` ended with `outcome` ("succeeded", "failed", "deferred", "lost"),
    or None. `params` are the params of the job that ended.

    - `ideas.pull` succeeded: a run (that also retries the deferred, with the pull's `limit` if it had one,
      and then it is the only run of the cycle) when files wait or a deferred item is due, else a publish
      when finished work is unpublished, else nothing. A pull that failed stops the cycle: the next
      scheduled pull tries again.
    - the run or a document job ended: a publish, but only when no document job can run now and no run is
      active. A delayed job (the YouTube gate, a retry delay) does not hold it back: when it ends later,
      this rule runs again. A failed run stops the chain (it could not read the inbox: more jobs would fail
      the same way). A preview is its own job type (`pipeline.preview`) and queues nothing.
    - `pipeline.publish` ended: a run (without retrying the deferred) when files are left, unless the publish
      says `follow_up` false (it follows a run with `limit`, `only` or `requeue`: at most one run) or the
      last run took no document at all (files the scan keeps leaving would loop for ever).
    - a backfill job (`backfill.docs`, `backfill.channel`) succeeded: a run (without retrying the deferred)
      only when it released at least one note, files wait in the inbox and no run is active, else nothing.
      A dry run, or a job that released nothing, queues nothing. Its end does not close a cycle.
    """
    if outcome == "lost":
        return None
    if job_type == PULL:
        if outcome != "succeeded":
            return None
        if not facts.inbox_empty or facts.deferred_due:
            return _run(retry_deferred=True, limit=params.get("limit"))
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
    if job_type in BACKFILL_JOBS:
        if outcome != "succeeded" or params.get("dry_run"):
            return None
        if not facts.backfill_released or facts.inbox_empty or facts.run_active:
            return None
        return _run(retry_deferred=False)
    return None


def end_of_chain(job_type: str, outcome: str, params: dict[str, Any], facts: Facts) -> str | None:
    """The log line that closes a cycle, when `job_type` ending with `outcome` leaves nothing more to queue
    (`decide` said None) and the cycle really is over; None while it goes on (other document jobs can still
    run, a run is active) or when the job is not part of the chain. It is a line for people: the emoji make
    the end of a cycle stand out between the lines of the jobs."""
    if outcome == "lost" or (job_type == PUBLISH and outcome == "deferred"):
        return None
    if job_type == PULL:
        if outcome == "succeeded":
            if facts.inbox_empty and not facts.deferred_due and not facts.unpublished_work:
                return "💤 chain idle: nothing to run or publish; the next ideas.pull looks again"
            return None
        return f"🛑 chain stopped: ideas.pull {outcome}; the next scheduled pull tries again"
    if job_type == RUN and outcome == "failed":
        return "🛑 chain stopped: pipeline.run failed; the next scheduled pull tries again"
    if job_type == PUBLISH:
        if facts.run_active:
            return None
        if outcome == "failed":
            return "⚠️ chain ended, the publish failed: the work stays committed, the next publish pushes it"
        if not params.get("follow_up", True):
            left = "" if facts.inbox_empty else "; the rest of the inbox waits for a run of its own"
            return f"🏆🏆🏆 chain complete (a run with a limit is one run){left}"
        if facts.inbox_empty:
            return "🏆🏆🏆 chain complete: all documents processed and published, the inbox is empty"
        if not facts.last_run_made_progress:
            return "🛑 chain stopped: files stay in inbox/, but the last run took none; the next pull retries"
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


# The facts each kind of job needs: reading only those keeps a backlog of documents from paying for an inbox
# scan and two git calls after every single document job.
_PULL_FACTS = {"inbox_empty", "deferred_due", "unpublished_work"}
_WORK_FACTS = {"document_jobs_runnable", "run_active", "latest_run_limited"}  # the run and its document jobs
_PUBLISH_FACTS = {"inbox_empty", "run_active", "last_run_made_progress"}
_BACKFILL_FACTS = {"inbox_empty", "run_active", "backfill_released"}


def _needed(job_type: str | None) -> set[str] | None:
    """The facts `decide` reads for `job_type`; None (all of them) when no type is given."""
    if job_type is None:
        return None
    if job_type == PULL:
        return _PULL_FACTS
    if job_type == PUBLISH:
        return _PUBLISH_FACTS
    if job_type in BACKFILL_JOBS:
        return _BACKFILL_FACTS
    return _WORK_FACTS if job_type == RUN or job_type in DOCUMENT_JOBS else set()


def read_facts(ctx: HandlerContext, job_type: str | None = None, job_id: uuid.UUID | None = None) -> Facts:
    """The facts `decide` needs, as they are now. Reads the inbox with the scan `pipeline.run` uses, so
    "empty" means: nothing the next run would take. With `job_type`, only the facts that type needs are read;
    the others keep a neutral value that `decide` does not look at. `job_id` is the job that ended: a backfill
    job's `backfill_released` comes from the `released` list of its stored result (committed before the chain
    runs; the claimed Job object does not carry it)."""
    wanted = _needed(job_type)

    def need(name: str) -> bool:
        return wanted is None or name in wanted

    now = ctx.clock()
    inbox_empty = True
    if need("inbox_empty"):
        scan = scan_inbox(ctx.ideas, now=now)
        inbox_empty = not (scan.notes or scan.artifacts or scan.errors)
    facts: dict[str, Any] = {
        "inbox_empty": inbox_empty,
        "chain_publish_push": ctx.settings.chain_publish_push,
    }
    with session_scope(ctx.engine) as session:
        if need("deferred_due"):
            retryable = [
                i for i in items_in_status(session, "deferred", "stuck") if i.origin != BACKFILL_ORIGIN
            ]
            orphaned = [
                i
                for i in items_in_status(session, *_RECOVERABLE_ACTIVE)
                if not live_job_carries(session, i.calculated_name)
            ]
            facts["deferred_due"] = bool(retryable or orphaned)
        if need("unpublished_work"):
            last_publish = session.scalar(
                select(func.max(Job.finished_at)).where(Job.type == PUBLISH, Job.status == "succeeded")
            )
            newer = select(func.count()).select_from(JobItem).where(JobItem.status == "published")
            if last_publish is not None:
                newer = newer.where(JobItem.updated_at > last_publish)
            facts["unpublished_work"] = bool(session.scalar(newer)) or _git_ahead(ctx)
        if need("document_jobs_runnable"):
            facts["document_jobs_runnable"] = count_runnable(session, now=now, types=DOCUMENT_JOBS)
        if need("run_active"):
            active = (
                select(func.count())
                .select_from(Job)
                .where(Job.type == RUN, Job.status.in_(("queued", "running")))
            )
            facts["run_active"] = bool(session.scalar(active))
        if need("backfill_released") and job_id is not None:
            stored = session.scalar(select(Job.result).where(Job.id == job_id))
            facts["backfill_released"] = bool((stored or {}).get("released"))
        if need("latest_run_limited") or need("last_run_made_progress"):
            runs = list(
                session.scalars(
                    select(Job).where(Job.type == RUN).order_by(Job.created_at.desc(), Job.id).limit(20)
                )
            )
            if need("latest_run_limited"):
                facts["latest_run_limited"] = bool(runs) and _limited(dict(runs[0].params or {}))
            if need("last_run_made_progress"):
                # the latest run that ended: one that failed took nothing, whatever an older run did
                ended_run = next((job for job in runs if job.status in ("succeeded", "failed")), None)
                facts["last_run_made_progress"] = (
                    ended_run is None or ended_run.status == "succeeded" and _made_progress(ended_run.result)
                )
    return Facts(**{**NEUTRAL, **facts})


def queue_next(ctx: HandlerContext, job: Job, outcome: str) -> Next | None:
    """After `job` ended with `outcome`: read the facts, ask `decide`, and queue what it says (one commit).
    Returns what was asked for (a job with that dedupe key may already have been active), or None."""
    if outcome == "lost":  # the result was discarded: decide would say nothing, so read nothing
        return None
    params = dict(job.params or {})
    facts = read_facts(ctx, job.type, job.id)
    chosen = decide(job.type, outcome, params, facts)
    if chosen is None:
        closing = end_of_chain(job.type, outcome, params, facts)
        if closing is not None:
            log.info("%s (after %s id=%s)", closing, job.type, job.id)
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


def start_pull(session: Session, *, now: datetime, params: dict[str, Any] | None = None) -> tuple[Job, bool]:
    """Queue the `ideas.pull` that starts a cycle of the chain, whoever asks (the schedule, `POST
    /pipeline/pull`): one function, one dedupe key (`chain:pull`), so a pull that is queued or running is
    returned instead of a second one (`created` is False, with the params of that earlier pull). `params`
    (`limit`) are checked by the handler's own parser (ValueError). Does not commit: the caller does."""
    given = dict(params or {})
    parse_pull_params(given)
    return enqueue(session, type=PULL, now=now, params=given, dedupe_key=PULL_KEY)
