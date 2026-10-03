"""Run one claimed job through its handler and finish it on the queue."""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Literal

from catcher.core.db import session_scope
from catcher.modules.queue import queue
from catcher.modules.queue.models import Job
from catcher.modules.worker.handlers import Defer, Done, Fail, Handler, HandlerContext, HandlerResult

log = logging.getLogger("catcher.worker")

Outcome = Literal["succeeded", "deferred", "failed", "lost"]


@contextmanager
def _heartbeat(ctx: HandlerContext, job: Job, *, lease_s: float, heartbeat_s: float) -> Iterator[None]:
    """Seam for the lease heartbeat. Currently a no-op; Task 2 replaces it with the real thread."""
    yield


def _run_handler(ctx: HandlerContext, job: Job, handlers: Mapping[str, Handler]) -> HandlerResult:
    handler = handlers.get(job.type)
    if handler is None:
        return Fail(f"no handler for {job.type}")
    try:
        result = handler(ctx, job)
    except Exception as e:  # KeyboardInterrupt and SystemExit are not Exceptions: they propagate
        log.exception("handler for job %s (%s) raised", job.id, job.type)
        return Fail(f"{type(e).__name__}: {e}")
    if not isinstance(result, (Done, Defer, Fail)):
        error = f"handler returned {type(result).__name__}, not a HandlerResult"
        log.error("job %s (%s): %s", job.id, job.type, error)
        return Fail(error)
    return result


def run_job(
    ctx: HandlerContext,
    job: Job,
    handlers: Mapping[str, Handler],
    *,
    lease_s: float,
    heartbeat_s: float,
) -> Outcome:
    """Run `job`'s handler, then finish the job in a fresh session. "lost" when the lease was no
    longer ours (the result is discarded). An error while finishing the job (a database error, or a handler
    that modified the Job it was given) propagates; the job then stays running and the reaper requeues it
    when its lease expires."""
    with _heartbeat(ctx, job, lease_s=lease_s, heartbeat_s=heartbeat_s):
        result = _run_handler(ctx, job, handlers)
    now = ctx.clock()
    with session_scope(ctx.engine) as session:
        match result:
            case Done(result=value):
                kept, outcome = queue.complete(session, job, now=now, result=value), "succeeded"
            case Defer(run_after=run_after, reason=reason):
                kept = queue.defer(session, job, now=now, run_after=run_after, reason=reason)
                outcome = "deferred"
            case Fail(error=error):
                kept, outcome = queue.fail(session, job, now=now, error=error), "failed"
    if not kept:
        log.warning("job %s (%s) lost its lease; the result was discarded", job.id, job.type)
        return "lost"
    return outcome
