"""Run one claimed job through its handler and finish it on the queue."""

import logging
import time
from collections.abc import Mapping
from typing import Literal

from catcher import __version__
from catcher.core.db import session_scope
from catcher.modules.queue import queue
from catcher.modules.queue.models import Job
from catcher.modules.worker.handlers import Defer, Done, Fail, Handler, HandlerContext, HandlerResult
from catcher.modules.worker.heartbeat import Heartbeat

log = logging.getLogger("catcher.worker")

Outcome = Literal["succeeded", "deferred", "failed", "lost"]

_END_EMOJI: dict[str, str] = {"succeeded": "✅", "deferred": "⏳", "failed": "❌", "lost": "⚠️"}


def _log_end(job: Job, outcome: Outcome, started: float) -> Outcome:
    """The line that closes a job in the log (the handlers' own lines sit between the start and this one)."""
    log.info(
        "%s job %s: %s id=%s catcher %s, %.1f s",
        _END_EMOJI[outcome],
        "done" if outcome == "succeeded" else outcome,
        job.type,
        job.id,
        __version__,
        time.monotonic() - started,
    )
    return outcome


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
    """Run `job`'s handler under a heartbeat that renews the lease every `heartbeat_s`, then finish the
    job in a fresh session. "lost" when the lease was no longer ours, seen by the heartbeat or by the
    fenced finish (the result is discarded; a job the heartbeat lost is not finished at all). An error
    while finishing the job (a database error, or a handler that modified the Job it was given) propagates;
    the job then stays running and the reaper requeues it when its lease expires.

    The log gets one line when the job starts and one when it ends, each with the job type, its id and the
    version, so the handlers' lines in between are easy to place."""
    started = time.monotonic()
    log.info("🚀 job start: %s id=%s catcher %s", job.type, job.id, __version__)
    with Heartbeat(ctx.engine, job, lease_s=lease_s, interval_s=heartbeat_s, clock=ctx.clock) as beat:
        result = _run_handler(ctx, job, handlers)
    if beat.lost:
        log.warning("job %s (%s) lost its lease while it ran; the result was discarded", job.id, job.type)
        return _log_end(job, "lost", started)
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
        return _log_end(job, "lost", started)
    return _log_end(job, outcome, started)
