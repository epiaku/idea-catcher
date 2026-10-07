"""`catcher run pipeline` over the worker: queue one `pipeline.run`, drain it with the `Worker`, publish.

The command holds the worker's Postgres lock (`WorkerLock`) for the whole run, so no worker runs beside it.
It queues `pipeline.run` with the user's options as params, runs the same `Worker` that `catcher worker
--once` runs until nothing is due (the run's own `youtube.fetch` and `llm.reason` jobs included), queues
`pipeline.publish` (`pull` = `push`: without `--push` only a local commit, no network) and runs it, then
builds the report from the database (`report_for_job`). The report also lists an earlier run's documents
that this run's drain worked on (their jobs were still queued) and those still waiting in the queue.

- **The lock** is checked before each job (`Worker.run_once`). A lost lock stops the run there: no further
  job, no publish, nothing committed; the outcome says what was done (`lock_lost`, exit 1).
- **Ctrl-C or SIGTERM** (KeyboardInterrupt): the jobs this command claimed and still runs are put back to
  `queued` with the queue's own fenced `defer` (the attempt is given back), and nothing is published. The
  half-done document stays where it is (its item and job say so); `catcher worker --once` finishes it.
- **An earlier run's `pipeline.run`/`pipeline.publish`** still queued or running (an interrupted run) would
  run here with its own params, so the command refuses to start (exit 2, nothing done); an earlier document
  job (`llm.reason`, `youtube.fetch`) is fine and runs too.
- **`wait_youtube_s`**: after the drain, while a `youtube.fetch` job of this run is queued and becomes due
  within `wait_youtube_s` (the gap of the YouTube gate), sleep until then and drain again. A clip whose wait
  is longer stays queued for a later `catcher worker`.
"""

import logging
import os
import socket
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg.errors import UndefinedTable
from sqlalchemy import Engine, func, or_, select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from catcher.core.config import Settings
from catcher.core.db import make_worker_engine, session_scope, utc_now
from catcher.modules.pipeline.process import Services
from catcher.modules.pipeline.report import RunReport, report_for_job
from catcher.modules.queue import queue
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.worker.app import JOB_RESOURCES, build_context, build_handlers, check_job
from catcher.modules.worker.guard import WorkerLock, WorkerLockLost
from catcher.modules.worker.handlers import Handler, HandlerContext, HandlerResult
from catcher.modules.worker.loop import Worker
from catcher.modules.youtube.gate_rules import clock_text

log = logging.getLogger("catcher.worker.runner")

LEASE_S = 120.0
INTERRUPTED = "interrupted: put back by catcher run pipeline (Ctrl-C or SIGTERM)"
PUBLISH_FAILED = "the changes are in the files but not (fully) committed or pushed"
PUSH_FAILED = "the changes are committed but not pushed: the next publish pushes them"


class RunDatabaseError(RuntimeError):
    """A database error stopped the run after it started (documents may already have moved)."""


class DatabaseNotUpgraded(RuntimeError):
    """The database has no tables yet (`catcher db upgrade` was never run): nothing was done."""


# The jobs that act on the whole inbox or on both repos: one left by an earlier run would run here with its
# own params (the whole inbox, a paid LLM call per document, or a push), so the command refuses to start.
RUN_WIDE_TYPES = ("pipeline.run", "pipeline.publish")
EARLIER_RUN = (
    "{n} job(s) from an earlier run are still queued (pipeline.run/pipeline.publish): finish them with "
    "catcher worker --once (see catcher jobs list), then run this again: nothing was done"
)


@dataclass
class RunOutcome:
    """What one `run pipeline` did. `blocked_lines` are the lines printed after the report: one per blocked
    LLM backend, and the clips that still wait for YouTube."""

    report: RunReport
    committed: dict[str, bool] = field(default_factory=dict)
    pushed: bool = False
    left_queued: int = 0
    interrupted: bool = False
    blocked_lines: list[str] = field(default_factory=list)
    exit_code: int = 0
    lock_lost: bool = False
    pushed_repos: list[str] = field(default_factory=list)  # with a failed push: the repos that were pushed
    push_failed: list[str] = field(default_factory=list)  # ... and the repos whose push failed
    published: bool = False  # the publish job succeeded (also when the lock was lost after it)
    refused: str | None = None  # why the run did not start (exit 2, nothing was done)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:run-{uuid.uuid4().hex[:8]}"


def _path_problems(ideas: Path, docs: Path) -> list[str]:
    """A missing inbox/ or docs folder, in Stage A's words; logged at ERROR."""
    problems = []
    for what, folder in (("idea-bucket inbox/", ideas / "inbox"), ("epiaku-docs", docs)):
        if not folder.is_dir():
            message = f"{what} not found at {folder}: check --ideas/--docs or IDEAS_REPO/DOCS_REPO"
            log.error(message)
            problems.append(message)
    return problems


def run_command(
    settings: Settings,
    *,
    ideas: Path,
    docs: Path,
    params: dict[str, Any],
    push: bool,
    wait_youtube_s: float | None,
    services: Services | None = None,
    services_factory: Callable[[Settings], Services] | None = None,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], object] = time.sleep,
) -> RunOutcome:
    """Run the inbox through the worker path (see the module docstring) and return what happened.

    Exit code 2 (nothing done, no database use) for a wrong path or bad params. Raises WorkerAlreadyRunning
    when a worker or another run holds the lock and OperationalError when the database cannot be reached
    while taking it (nothing was done); RunDatabaseError when a database error stops the run later.
    `services_factory` builds the services after the lock is taken (the command's real ones); `services`
    are used as they are; with neither, the worker's own (`build_context`)."""
    problems = _path_problems(ideas, docs)
    try:
        check_job("pipeline.run", params)
    except ValueError as e:
        log.error("cannot run: %s", e)
        problems.append(f"cannot run: {e}")
    if problems:
        return RunOutcome(report=RunReport(problems=problems), exit_code=2)

    def body(ctx: HandlerContext, worker: Worker, worked_on: list[str]) -> RunOutcome:
        return _run(
            ctx, worker, params, push=push, wait_youtube_s=wait_youtube_s, sleep=sleep, worked_on=worked_on
        )

    return _under_the_lock(
        settings,
        ideas=ideas,
        docs=docs,
        services=services,
        services_factory=services_factory,
        clock=clock,
        body=body,
    )


def publish_command(
    settings: Settings,
    *,
    ideas: Path,
    docs: Path,
    push: bool,
    services: Services | None = None,
    services_factory: Callable[[Settings], Services] | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> RunOutcome:
    """`catcher publish`: queue ONE `pipeline.publish` (`pull` = `push`), never a `pipeline.run`, under the
    same lock and refusals as `run_command`, drain it and return what it committed and pushed (`committed`,
    `pushed`, `published`; git's push problems in `report.problems`). Raises what `run_command` raises."""
    problems = _path_problems(ideas, docs)
    if problems:
        return RunOutcome(report=RunReport(problems=problems), exit_code=2)
    return _under_the_lock(
        settings,
        ideas=ideas,
        docs=docs,
        services=services,
        services_factory=services_factory,
        clock=clock,
        body=lambda ctx, worker, worked_on: _publish_only(ctx, worker, push=push),
    )


def _under_the_lock(
    settings: Settings,
    *,
    ideas: Path,
    docs: Path,
    services: Services | None,
    services_factory: Callable[[Settings], Services] | None,
    clock: Callable[[], datetime],
    body: Callable[[HandlerContext, Worker, list[str]], RunOutcome],
) -> RunOutcome:
    """Take the worker lock, refuse over an earlier run's `pipeline.run`/`pipeline.publish`, build the
    context and a `Worker` that checks the lock before each job, and return `body(ctx, worker, worked_on)`."""
    with ExitStack() as stack:
        lock_engine = make_worker_engine(settings.database_url)
        stack.callback(lock_engine.dispose)
        lock = stack.enter_context(WorkerLock(lock_engine))  # WorkerAlreadyRunning / OperationalError
        earlier = _earlier_run_jobs(lock_engine)
        if earlier:
            log.error(EARLIER_RUN.format(n=earlier))
            return RunOutcome(report=RunReport(), refused=EARLIER_RUN.format(n=earlier), exit_code=2)
        if services is None and services_factory is not None:
            services = services_factory(settings)
        ctx = build_context(settings, ideas=ideas, docs=docs, clock=clock, services=services)
        stack.callback(ctx.engine.dispose)
        worked_on: list[str] = []
        worker = Worker(
            ctx,
            _recording(build_handlers(), worked_on),
            worker_id=_worker_id(),
            lease_s=LEASE_S,
            heartbeat_s=LEASE_S / 3,
            lock_check=lock.check,
        )
        try:
            return body(ctx, worker, worked_on)
        except SQLAlchemyError as e:
            log.error("a database error stopped the run: %s", getattr(e, "orig", None) or type(e).__name__)
            raise RunDatabaseError(str(type(e).__name__)) from e
    raise AssertionError("unreachable")  # the with block returns or raises; ExitStack could swallow in theory


def _read_publish(outcome: RunOutcome, publish: Job | None, *, stopped: bool) -> bool:
    """Put the `pipeline.publish` job's result in `outcome`; True when the job failed or did not finish
    (and the run was not stopped before: a lock lost or Ctrl-C leaves its own message)."""
    if publish is None:
        return False
    if publish.status == "succeeded":  # it committed (and pushed), even if the lock went after
        result = publish.result or {}
        outcome.published = True
        outcome.committed = dict(result.get("committed") or {})
        outcome.pushed = bool(result.get("pushed", False))
        outcome.pushed_repos = list(result.get("pushed_repos") or [])
        outcome.push_failed = list(result.get("push_failed") or [])
        for problem in result.get("problems") or []:  # a failed push: committed, not pushed
            outcome.report.problems.append(f"{problem}: {PUSH_FAILED}")
        return False
    if stopped:
        return False
    why = publish.error or f"pipeline.publish is {publish.status}"
    outcome.report.problems.append(f"{why}: {PUBLISH_FAILED}")
    return True


def _publish_only(ctx: HandlerContext, worker: Worker, *, push: bool) -> RunOutcome:
    """Queue and run one `pipeline.publish`; the outcome says what is true (the job's result, or why not)."""
    outcome = RunOutcome(report=RunReport())
    publish_id: uuid.UUID | None = None
    job_errors = 0
    try:
        worker.check_lock()
        worker.reap_safely()
        publish_id = _enqueue(ctx, "pipeline.publish", {"push": push, "pull": push})
        job_errors += _drain(worker)[1]
    except WorkerLockLost as e:
        log.error("the lock was lost: the publish stops here (%s)", e)
        outcome.lock_lost = True
    except KeyboardInterrupt:
        released = _release_own_jobs(ctx, worker.worker_id)
        log.warning("interrupted: %d running job(s) put back in the queue", released)
        outcome.interrupted = True
    failed = False
    if publish_id is not None:
        with session_scope(ctx.engine) as session:
            publish = session.get(Job, publish_id)
            failed = _read_publish(outcome, publish, stopped=outcome.lock_lost or outcome.interrupted)
            outcome.left_queued = int(publish is not None and publish.status == "queued")
    outcome.report.committed, outcome.report.pushed = outcome.committed, outcome.pushed
    stopped = outcome.lock_lost or outcome.interrupted or failed or job_errors > 0
    outcome.exit_code = 1 if outcome.report.problems or stopped else 0
    return outcome


def _recording(handlers: dict[str, Handler], worked_on: list[str]) -> dict[str, Handler]:
    """The handlers, each first noting the document its job works on (`calculated_name`) in `worked_on`, so
    the report also lists an earlier run's documents whose queued jobs this run's drain ran."""

    def record(handler: Handler) -> Handler:
        def run(ctx: HandlerContext, job: Job) -> HandlerResult:
            name = (job.params or {}).get("calculated_name")
            if isinstance(name, str) and name not in worked_on:
                worked_on.append(name)
            return handler(ctx, job)

        return run

    return {job_type: record(handler) for job_type, handler in handlers.items()}


def _earlier_run_jobs(engine: Engine) -> int:
    """How many `pipeline.run`/`pipeline.publish` jobs are queued or running before this run queues its own.
    Raises DatabaseNotUpgraded when the database has no tables."""
    statement = (
        select(func.count())
        .select_from(Job)
        .where(Job.type.in_(RUN_WIDE_TYPES), Job.status.in_(("queued", "running")))
    )
    try:
        with session_scope(engine) as session:
            return int(session.scalar(statement) or 0)
    except ProgrammingError as e:
        if isinstance(e.orig, UndefinedTable):
            raise DatabaseNotUpgraded("the database has no tables yet: run catcher db upgrade") from e
        raise


def _run(
    ctx: HandlerContext,
    worker: Worker,
    params: dict[str, Any],
    *,
    push: bool,
    wait_youtube_s: float | None,
    sleep: Callable[[float], object],
    worked_on: list[str],
) -> RunOutcome:
    run_id: uuid.UUID | None = None
    publish_id: uuid.UUID | None = None
    outcome = RunOutcome(report=RunReport())
    job_errors = 0
    try:
        worker.check_lock()
        worker.reap_safely()
        run_id = _enqueue(ctx, "pipeline.run", params)
        job_errors += _drain(worker)[1]
        if wait_youtube_s is not None:
            job_errors += _wait_for_youtube(ctx, worker, run_id, wait_youtube_s, sleep)
        publish_id = _enqueue(ctx, "pipeline.publish", {"push": push, "pull": push})
        job_errors += _drain(worker)[1]
    except WorkerLockLost as e:
        log.error("the run lock was lost: the run stops here and commits nothing (%s)", e)
        outcome.lock_lost = True
    except KeyboardInterrupt:
        released = _release_own_jobs(ctx, worker.worker_id)
        log.warning("interrupted: %d running job(s) put back in the queue", released)
        outcome.interrupted = True

    now = ctx.clock()
    with session_scope(ctx.engine) as session:
        if run_id is not None:
            names = _run_item_names(session, run_id)
            # an earlier run's documents: the ones this drain worked on, and the ones still waiting in the
            # queue (a closed YouTube gate), as Stage A reported every waiting clip on each run
            earlier = list(dict.fromkeys(n for n in [*worked_on, *_waiting_names(session)] if n not in names))
            outcome.report = report_for_job(session, run_id, also=earlier)
            names += earlier
            outcome.left_queued = _queued_jobs_of_run(session, run_id, names, publish_id)
            outcome.blocked_lines = _blocked_lines(ctx, session, names, now)
            outcome.blocked_lines += _youtube_wait_line(session, names, now)
        publish = session.get(Job, publish_id) if publish_id is not None else None
        publish_failed = _read_publish(outcome, publish, stopped=outcome.lock_lost or outcome.interrupted)
    outcome.report.committed, outcome.report.pushed = outcome.committed, outcome.pushed
    report = outcome.report
    failed = (
        bool(report.unreadable or report.not_found or report.not_in_archive or report.problems)
        or report.counts().get("failed", 0) > 0
    )
    stopped = outcome.lock_lost or outcome.interrupted or publish_failed or job_errors > 0
    outcome.exit_code = 1 if failed or stopped else 0
    return outcome


def _enqueue(ctx: HandlerContext, job_type: str, params: dict[str, Any]) -> uuid.UUID:
    """Queue one job, due now, without a dedupe key (an older queued run never swallows this one)."""
    with session_scope(ctx.engine) as session:
        job, _ = queue.enqueue(
            session, type=job_type, now=ctx.clock(), params=params, resource=JOB_RESOURCES.get(job_type)
        )
        return job.id


def _drain(worker: Worker) -> tuple[int, int]:
    """Run due jobs until none is left (the jobs they queue included). Returns how many ran and how many of
    them could not be finished ("error": they stay running until the reaper requeues them). A claim that
    hit a database error stops the run."""
    ran = errors = 0
    while True:
        result = worker.run_once()
        if result is None:
            return ran, errors
        if result == "claim_error":
            raise RunDatabaseError("could not claim a job")
        ran += 1
        errors += result == "error"


def _release_own_jobs(ctx: HandlerContext, worker_id: str) -> int:
    """Put every job this command claimed and still runs back to `queued`, due now, with the fenced
    `queue.defer` (the attempt is given back). Only this command runs jobs while it holds the lock, and its
    worker id is its own, so these are exactly the jobs Ctrl-C cut off."""
    now = ctx.clock()
    released = 0
    with session_scope(ctx.engine) as session:
        running = session.scalars(select(Job).where(Job.status == "running", Job.locked_by == worker_id))
        for job in list(running):
            if queue.defer(session, job, now=now, run_after=now, reason=INTERRUPTED):
                released += 1
    return released


def _run_item_names(session: Session, run_id: uuid.UUID) -> list[str]:
    """The calculated names of this run's items: staged by it, or adopted by it."""
    run = session.get(Job, run_id)
    adopted = list(((run.result or {}).get("names") or {}).get("adopted", [])) if run is not None else []
    return list(
        session.scalars(
            select(JobItem.calculated_name).where(
                or_(JobItem.root_job_id == run_id, JobItem.calculated_name.in_(adopted))
            )
        )
    )


def _waiting_names(session: Session) -> list[str]:
    """The documents whose `youtube.fetch` or `llm.reason` job is still queued (any run's)."""
    statement = select(Job.params["calculated_name"].astext).where(
        Job.status == "queued", Job.type.in_(("youtube.fetch", "llm.reason"))
    )
    return [name for name in session.scalars(statement) if name]


def _of_run(run_id: uuid.UUID, names: list[str], publish_id: uuid.UUID | None) -> Any:
    ids = [run_id, *([publish_id] if publish_id is not None else [])]
    return or_(Job.id.in_(ids), Job.params["calculated_name"].astext.in_(names))


def _queued_jobs_of_run(
    session: Session, run_id: uuid.UUID, names: list[str], publish_id: uuid.UUID | None
) -> int:
    """The jobs of this run (its own, its publish, and the jobs of its items) still queued."""
    statement = (
        select(func.count())
        .select_from(Job)
        .where(Job.status == "queued", _of_run(run_id, names, publish_id))
    )
    return int(session.scalar(statement) or 0)


def _fetch_due_times(session: Session, names: list[str], now: datetime) -> list[datetime]:
    """When each queued `youtube.fetch` job of this run can be claimed: its `run_after`, or later while the
    YouTube gate holds it."""
    rows = session.execute(
        select(Job.run_after, queue.resource_closed_until(now)).where(
            Job.status == "queued",
            Job.type == "youtube.fetch",
            Job.params["calculated_name"].astext.in_(names),
        )
    ).all()
    return [max(run_after, closed) if closed is not None else run_after for run_after, closed in rows]


def _wait_for_youtube(
    ctx: HandlerContext, worker: Worker, run_id: uuid.UUID, wait_s: float, sleep: Callable[[float], object]
) -> int:
    """While a `youtube.fetch` job of this run becomes due within `wait_s` from now, sleep until then and
    drain again. Returns how many jobs could not be finished."""
    errors = 0
    while True:
        now = ctx.clock()
        with session_scope(ctx.engine) as session:
            due = _fetch_due_times(session, _run_item_names(session, run_id), now)
        if not due:
            return errors
        wait = max((min(due) - now).total_seconds(), 0.0)
        if wait > wait_s:
            return errors
        log.info("waiting %.0f s for the YouTube gate (--wait-youtube)", wait)
        sleep(wait)
        ran, failed = _drain(worker)
        errors += failed
        if ran == 0 and wait == 0:  # due, yet nothing could be claimed: waiting longer changes nothing
            return errors


def _youtube_wait_line(session: Session, names: list[str], now: datetime) -> list[str]:
    """`N clip(s) wait for YouTube until HH:MM: run catcher worker`, for the fetch jobs left queued."""
    due = _fetch_due_times(session, names, now)
    if not due:
        return []
    until = clock_text(max(due).timestamp(), now.timestamp())
    return [f"{len(due)} clip(s) wait for YouTube until {until}: run catcher worker"]


def _blocked_lines(ctx: HandlerContext, session: Session, names: list[str], now: datetime) -> list[str]:
    """One line per blocked LLM backend (or profile): until when, why, and how many documents of this run
    it deferred (`deferred` items that tried that backend)."""
    lines = []
    for key, block in sorted(ctx.backend_blocks.entries(now).items()):
        backend, _, profile = key.partition(":")
        statement = (
            select(func.count())
            .select_from(JobItem)
            .where(
                JobItem.calculated_name.in_(names),
                JobItem.status == "deferred",
                JobItem.llm_backend == backend,
            )
        )
        if profile:
            statement = statement.where(JobItem.llm_profile == profile)
        deferred = int(session.scalar(statement) or 0)
        reason = (block.cause.strip().splitlines() or [""])[0]
        until = clock_text(block.until.timestamp(), now.timestamp())
        lines.append(f"{key} blocked until {until} ({reason}): {deferred} document(s) deferred")
    return lines
