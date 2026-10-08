import logging
import math
import os
import re
import secrets
import signal
import socket
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import typer
from alembic import command as alembic_command
from croniter import croniter
from dotenv import load_dotenv
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from catcher import __version__
from catcher.core.config import Settings
from catcher.core.db import alembic_config, make_worker_engine, session_scope, utc_now
from catcher.core.log import configure_logging, share_project_handlers
from catcher.core.testdata import DEFAULT_SOURCE, DEFAULT_TARGET, TestDataError, reset_test_repos
from catcher.modules.backfill import channel as backfill_channel
from catcher.modules.backfill import store as backfill_store
from catcher.modules.backfill.importer import run_import
from catcher.modules.backfill.release import daily_allowance, release
from catcher.modules.backfill.scan import known_ids
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.profiles import UnknownProfile, load_profiles, resolve_profile
from catcher.modules.llm.service import LlmError, LlmRequest, reason
from catcher.modules.pipeline.context import load_context
from catcher.modules.pipeline.glossary import load_glossary
from catcher.modules.pipeline.inbox import (
    Note,
    calculated_stem,
    inbox_root,
    is_snapshot_of,
    name_title,
    read_note,
    scan_inbox,
)
from catcher.modules.pipeline.inputs import prompt_input
from catcher.modules.pipeline.preview import preview
from catcher.modules.pipeline.process import ProcessOptions, Services, default_services, process_note
from catcher.modules.pipeline.publish import write_page
from catcher.modules.pipeline.report import NOT_STARTED, RunReport
from catcher.modules.pipeline.scan_state import scan_item_files
from catcher.modules.pipeline.tags import load_tags
from catcher.modules.queue import queue
from catcher.modules.queue.models import ITEM_STATUSES, JOB_STATUSES, Job, JobItem, Schedule
from catcher.modules.queue.reconcile import reconcile
from catcher.modules.scheduler.loop import Scheduler
from catcher.modules.scheduler.schedule import (
    SCHEDULE_NAMES,
    ScheduleSpec,
    due_slot,
    load_timezone,
    parse_schedules,
)
from catcher.modules.worker.app import JOB_RESOURCES, build_context, build_handlers, check_job
from catcher.modules.worker.guard import WorkerAlreadyRunning, WorkerLock, WorkerLockLost, worker_running
from catcher.modules.worker.loop import Worker
from catcher.modules.worker.runner import (
    DatabaseNotUpgraded,
    RunDatabaseError,
    RunOutcome,
    publish_command,
    run_command,
)
from catcher.modules.youtube.access import YoutubeAccess, build_access
from catcher.modules.youtube.cache import FACTS_DIR
from catcher.modules.youtube.facts import FactsDeferred, FactsUnavailable
from catcher.modules.youtube.gate import GateUnavailable
from catcher.modules.youtube.gate_rules import GateState, clock_text, wait_for
from catcher.modules.youtube.pg_gate import PostgresGate
from catcher.modules.youtube.urls import video_id

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,  # a traceback must never print DATABASE_URL or a key from Settings
    help="Idea Catcher: turns idea-bucket captures into epiaku-docs pages.",
)

IdeasOpt = Annotated[Path | None, typer.Option("--ideas", help="idea-bucket checkout (default: IDEAS_REPO)")]
DocsOpt = Annotated[Path | None, typer.Option("--docs", help="epiaku-docs checkout (default: DOCS_REPO)")]
FileOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--file",
        "-f",
        help="process only this document (file name, name without .md, or subfolder/name). Repeat for more",
    ),
]
RequeueOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--requeue",
        help="copy this document from archive/ back into inbox/, then process it again "
        "(same name forms as --file). Repeat for more",
    ),
]
ProfileOpt = Annotated[
    str | None,
    typer.Option(
        "--profile", "--llm-profile", help="LLM profile from profiles.yaml: notes, clippings or youtube"
    ),
]


@app.callback()
def main(
    log_level: Annotated[
        str | None, typer.Option("--log-level", help="DEBUG, INFO, WARNING or ERROR (default: LOG_LEVEL)")
    ] = None,
) -> None:
    load_dotenv(override=False)
    settings = Settings()
    try:
        configure_logging(log_level or settings.log_level, settings.log_file)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def scan(ideas: IdeasOpt = None, file: FileOpt = None) -> None:
    """List what is in inbox/ (class, id, duplicates) without changing anything."""
    settings = Settings()
    _check_ideas_inbox(ideas or settings.ideas_repo)
    result = scan_inbox(ideas or settings.ideas_repo, only=file)
    winners: dict[str, Note] = {}
    for note in result.notes:
        best = winners.get(note.doc_id)
        if best is None or len(note.doc.body) > len(best.doc.body):
            winners[note.doc_id] = note
    for note in result.notes:
        winner = winners[note.doc_id]
        dup = note is not winner and is_snapshot_of(note.doc.body, winner.doc.body)
        status = f"duplicate of {winner.rel.as_posix()}" if dup else "would process"
        typer.echo(f"{status:<14} {note.doctype.name:<15} {note.doc_id:<24} <- {note.doc.fm['source_file']}")
    for artifact in result.artifacts:
        size = f"{artifact.size / 1024:,.0f} KB"
        too_big = artifact.size > settings.artifact_max_mb * 1024 * 1024
        status = "would skip" if too_big else "would copy"  # over ARTIFACT_MAX_MB it stays in inbox/
        typer.echo(f"{status:<14} {'artifact':<15} {size:<24} <- {artifact.path.name}")
    for rel, error in result.errors.items():
        typer.echo(f"{'unreadable':<14} {rel}: {error}", err=True)
    missing = [q for q in file or [] if q not in result.matched]
    for query in missing:
        typer.echo(f'{"not-found":<14} no document named "{query}" in inbox/', err=True)
    raise typer.Exit(1 if result.errors or missing else 0)


log = logging.getLogger("catcher.cli")


def _read_document(path: Path) -> Note:
    """Read the document `reason` and `render` were pointed at. A problem is logged as an error (to the
    terminal and to LOG_FILE) and ends the command with exit code 2, not with a traceback."""
    try:
        return read_note(path)
    except FileNotFoundError:
        log.error("no such file: %s", path)
    except (OSError, UnicodeDecodeError, ValueError) as e:  # ValueError includes a bad frontmatter
        log.error("cannot read %s: %s", path, e)
    raise typer.Exit(2)


def _facts_dir_of(document: Path) -> Path | None:
    """Where `render` may save YouTube facts: `facts/` of the idea-bucket the document is in. A document
    that is not in an `inbox/` gets no saving, so a stray path never writes into the wrong repo."""
    root = inbox_root(document)
    return root.parent / FACTS_DIR if root else None


def _check_ideas_inbox(ideas: Path) -> None:
    if not (ideas / "inbox").is_dir():
        log.error("no inbox/ folder in %s: check --ideas or IDEAS_REPO", ideas)
        raise typer.Exit(2)


@app.command("reason")
def reason_cmd(document: Path, profile: ProfileOpt = None) -> None:
    """Run the LLM step on one document and print the validated JSON. Writes nothing."""
    settings = Settings()
    note = _read_document(document)
    if note.doctype.name == "youtube":
        typer.echo("youtube notes need facts first: use `catcher render` for them.")
        raise typer.Exit(2)
    profiles = load_profiles(settings.profiles_file)
    try:
        name, _ = resolve_profile(profiles, requested=profile, class_default=note.doctype.llm_profile)
    except UnknownProfile as e:
        typer.echo(f"profile problem: {e}", err=True)
        raise typer.Exit(2) from e
    request = LlmRequest(
        task=note.doctype.task,
        input=prompt_input(note, load_tags(), glossary=load_glossary(), context=load_context()),
        schema_name=note.doctype.schema_name,
        profile=name,
    )
    try:
        result = reason(request, profiles=profiles, backends=lambda p: make_backend(p, settings))
    except LlmError as e:
        typer.echo(f"LLM step failed: {e}", err=True)
        raise typer.Exit(2) from e
    typer.echo(result.output.model_dump_json(indent=2))
    typer.echo(
        f"profile={result.profile} backend={result.backend} model={result.model} "
        f"prompt={result.prompt_version} attempts={result.attempts} "
        f"tokens_in={result.usage.tokens_in} tokens_out={result.usage.tokens_out}",
        err=True,
    )


@app.command()
def render(
    document: Path,
    docs: DocsOpt = None,
    profile: ProfileOpt = None,
) -> None:
    """Summarize one document and write its page into the docs checkout (no inbox change, no git).

    It needs the database: it takes the run lock in DATABASE_URL first, like `run pipeline`, so no worker
    commits (and pushes) the page while it is written. Exit codes: 0 written; 1 the page is invalid; 2 a
    wrong path, the LLM step failed, YouTube facts are unavailable, DATABASE_URL is malformed, the database
    cannot be reached, or a worker or a run is running (nothing was done)."""
    settings = Settings()
    _check_database_url(settings.database_url)  # the run lock and a YouTube clip's gate are in the database
    docs_repo = docs or settings.docs_repo
    note = _read_document(document)
    note.name = f"{calculated_stem(str(note.doc.fm['captured']), secrets.token_hex(3), name_title(note))}.md"
    opts = ProcessOptions(profile=profile, facts_dir=_facts_dir_of(document))
    with ExitStack() as stack:
        _hold_the_run_lock(stack, settings)  # before facts are saved or a page is written
        try:
            processed = process_note(note, _services(stack, settings), opts)
        except (LlmError, UnknownProfile) as e:
            typer.echo(f"LLM step failed: {e}", err=True)
            raise typer.Exit(2) from e
        except FactsUnavailable as e:
            typer.echo(f"YouTube facts unavailable: {e}", err=True)
            raise typer.Exit(2) from e
        for problem in processed.problems:
            typer.echo(f"problem: {problem}", err=True)
        if processed.problems:
            raise typer.Exit(1)
        touched = write_page(docs_repo, note.destination, note.doc_id, processed.filename, processed.page)
    typer.echo(f"wrote   {touched[0]}")
    for old in touched[1:]:
        typer.echo(f"removed {old}")


run_app = typer.Typer(no_args_is_help=True, help="Run a whole flow.")
app.add_typer(run_app, name="run")


@run_app.callback()
def run_group() -> None:
    """Run a whole flow."""


def _terminate(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


@run_app.command("pipeline")
def run_pipeline_cmd(
    ideas: IdeasOpt = None,
    docs: DocsOpt = None,
    profile: ProfileOpt = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="change no files, commit nothing")] = False,
    push: Annotated[bool, typer.Option("--push", help="push both repos (off by default)")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="process at most N notes")] = None,
    file: FileOpt = None,
    requeue: RequeueOpt = None,
    refresh_facts: Annotated[
        bool, typer.Option("--refresh-facts", help="fetch the YouTube facts again even when they are saved")
    ] = False,
    wait_youtube: Annotated[
        bool,
        typer.Option(
            "--wait-youtube",
            help="when a clip must wait for the gap between YouTube calls, wait (up to YOUTUBE_WAIT_MAX_S) "
            "instead of leaving its fetch queued for a later `catcher worker`",
        ),
    ] = False,
    retry_deferred: Annotated[
        bool,
        typer.Option(
            "--retry-deferred",
            help="first put the documents a temporary error stalled (in output/, stage deferred) back into "
            "inbox/, so they run again without a --requeue per document",
        ),
    ] = False,
    refresh_llm: Annotated[
        bool,
        typer.Option("--refresh-llm", help="call the LLM again even when a good reply is saved in llm/"),
    ] = False,
) -> None:
    """Process the documents in inbox/: publish pages, file failures and duplicates, and commit.

    It runs the worker path in this process: it takes the worker's lock in DATABASE_URL (also for --dry-run)
    and holds it until the run ends, queues a `pipeline.run` job with the options, runs the worker until no
    job is due, then queues and runs `pipeline.publish` (without --push it only commits: no pull, no push).
    Ctrl-C or a `kill` puts the job it was running back in the queue and publishes nothing: `catcher worker
    --once` finishes it. It refuses to start while a `pipeline.run` or `pipeline.publish` job of an earlier
    run is still queued (run `catcher worker --once` first).

    Exit codes: 0 done; 1 a document failed, a name was not found, a file was unreadable, the publish failed,
    the run was interrupted, or it lost its database lock (it stopped before the next job and committed
    nothing); 2 a wrong path, DATABASE_URL is malformed, the database cannot be reached or has no tables, a
    worker or another run is running, or an earlier run's job is still queued (nothing was done)."""
    settings = Settings()
    signal.signal(signal.SIGTERM, _terminate)  # a `kill` ends the run like Ctrl-C: the job goes back
    ideas_repo, docs_repo = ideas or settings.ideas_repo, docs or settings.docs_repo
    _check_database_url(settings.database_url)
    params = _run_params(profile, limit, file, requeue, retry_deferred, refresh_llm, refresh_facts)
    if dry_run:  # a read-only preview of its own: no job, no row, no file
        _dry_run(settings, ideas_repo, docs_repo, params)
        return
    with ExitStack() as stack, _worker_path_errors():
        outcome = run_command(
            settings,
            ideas=ideas_repo,
            docs=docs_repo,
            params=params,
            push=push,
            wait_youtube_s=settings.youtube_wait_max_s if wait_youtube else None,
            services_factory=lambda s: _services(stack, s),  # built while the lock is held
        )
    if outcome.refused:
        typer.echo(outcome.refused, err=True)
        raise typer.Exit(outcome.exit_code)
    report = outcome.report
    _print_items(report)
    for line in outcome.blocked_lines:
        typer.echo(line)
    if outcome.lock_lost and outcome.published:  # the publish committed (and pushed) before the lock went
        typer.echo(f"summary: {report.counts()} committed={outcome.committed} pushed={outcome.pushed}")
        log.error("%s", RUN_LOST_AFTER_PUBLISH)
        typer.echo(RUN_LOST_AFTER_PUBLISH, err=True)
    elif outcome.lock_lost:
        typer.echo(f"{_changed_by_the_run(report)} were finished and are NOT committed", err=True)
        log.error("%s", RUN_LOST)
        typer.echo(RUN_LOST, err=True)
    elif outcome.interrupted:
        typer.echo(
            f"interrupted: {outcome.left_queued} job(s) left queued, run catcher worker --once to finish",
            err=True,
        )
    else:
        typer.echo(f"summary: {report.counts()} committed={outcome.committed} pushed={outcome.pushed}")
    raise typer.Exit(outcome.exit_code)


@contextmanager
def _worker_path_errors() -> Iterator[None]:
    """The exits of a command that runs the worker path (`run_command`, `publish_command`): 2 when a worker
    or run runs, the database cannot be reached or has no tables (nothing was done); 1 when a database error
    stopped it later."""
    try:
        yield
    except WorkerAlreadyRunning as e:
        log.error("%s", RUN_BUSY)
        typer.echo(RUN_BUSY, err=True)
        raise typer.Exit(2) from e
    except OperationalError as e:  # taking the lock: nothing was done
        log.error("cannot reach the database in DATABASE_URL: %s", e.orig or e)
        typer.echo("cannot reach the database in DATABASE_URL: nothing was done", err=True)
        raise typer.Exit(2) from e
    except DatabaseNotUpgraded as e:
        log.error("%s", e)
        typer.echo(f"{e}: nothing was done", err=True)
        raise typer.Exit(2) from e
    except RunDatabaseError as e:
        typer.echo(RUN_DB_ERROR, err=True)
        raise typer.Exit(1) from e


@app.command()
def publish(
    ideas: IdeasOpt = None,
    docs: DocsOpt = None,
    push: Annotated[bool, typer.Option("--push", help="pull and push both repos (off by default)")] = False,
) -> None:
    """Commit the managed folders of both repos by hand (and with --push pull and push them).

    It takes the worker's lock in DATABASE_URL like `run pipeline`, queues ONE `pipeline.publish` job (never
    a `pipeline.run`), runs it and prints what was committed and pushed per repo. Without --push it only
    commits: no pull, no push. Nothing to commit says so and exits 0. Ctrl-C or a `kill` puts the job back
    in the queue (`catcher worker --once` finishes it).

    Exit codes: 0 done; 1 the publish failed, a push failed (the commit stays local), it was interrupted, or
    the lock was lost (it says whether the publish happened); 2 DATABASE_URL is malformed, the database
    cannot be reached or has no tables, a worker or a run is running, or an earlier `pipeline.run` or
    `pipeline.publish` job is still queued (nothing was done)."""
    settings = Settings()
    signal.signal(signal.SIGTERM, _terminate)
    _check_database_url(settings.database_url)
    with ExitStack() as stack, _worker_path_errors():
        outcome = publish_command(
            settings,
            ideas=ideas or settings.ideas_repo,
            docs=docs or settings.docs_repo,
            push=push,
            services_factory=lambda s: _services(stack, s),
        )
    if outcome.refused:
        typer.echo(outcome.refused, err=True)
        raise typer.Exit(outcome.exit_code)
    for problem in outcome.report.problems:
        typer.echo(f"problem: {problem}", err=True)
    if outcome.published:
        typer.echo(_publish_lines(outcome, push))
    again = "run catcher worker --once to finish it" if outcome.left_queued else "nothing was queued or left"
    if outcome.lock_lost and outcome.published:
        typer.echo("the lock was lost after the publish: the publish itself is done (see above)", err=True)
    elif outcome.lock_lost:
        typer.echo(f"the lock was lost before the publish ran: nothing was committed; {again}", err=True)
    elif outcome.interrupted and outcome.published:
        typer.echo("interrupted after the publish: the publish itself is done (see above)", err=True)
    elif outcome.interrupted:
        typer.echo(f"interrupted before the publish finished: nothing was committed; {again}", err=True)
    raise typer.Exit(outcome.exit_code)


def _publish_lines(outcome: RunOutcome, push: bool) -> str:
    """What the publish did per repo; `nothing to commit` only when nothing was committed or pushed and no
    push failed."""
    committed = outcome.committed

    def yes(done: bool) -> str:
        return "yes" if done else "no"

    made = f"committed: ideas {yes(committed.get('ideas', False))}, docs {yes(committed.get('docs', False))}"
    if outcome.push_failed:  # the commit is local; say per repo what was pushed and what not
        pushed = ", ".join(outcome.pushed_repos) or "none"
        failed = ", ".join(outcome.push_failed)
        return f"{made}\npushed: {pushed}; push FAILED for: {failed} (committed, not pushed)"
    if not any(committed.values()) and not outcome.pushed:
        return "nothing to commit" + (": nothing to push either" if push else "")
    pushed_text = yes(outcome.pushed) if push else "no (without --push)"
    return f"{made}\npushed: {pushed_text}"


def _run_params(
    profile: str | None,
    limit: int | None,
    file: list[str] | None,
    requeue: list[str] | None,
    retry_deferred: bool,
    refresh_llm: bool,
    refresh_facts: bool,
) -> dict[str, Any]:
    """The `pipeline.run` params of the command's options: only the ones given."""
    given: dict[str, Any] = {"limit": limit, "only": file, "requeue": requeue, "profile": profile}
    params = {key: value for key, value in given.items() if value is not None}
    flags = {"retry_deferred": retry_deferred, "refresh_llm": refresh_llm, "refresh_facts": refresh_facts}
    params.update({key: True for key, on in flags.items() if on})
    return params


def _dry_run(settings: Settings, ideas: Path, docs: Path, params: dict[str, Any]) -> None:
    """`run pipeline --dry-run`: a read-only preview (`preview`). It holds the run lock like a run does, so a
    worker's files are not read halfway through a change, and writes nothing."""
    with ExitStack() as stack:
        _hold_the_run_lock(stack, settings)
        report = preview(ideas, docs, params, _services(stack, settings))
    _print_items(report)
    typer.echo(f"summary: {report.counts()} committed={report.committed} pushed={report.pushed}")
    if report.problems:
        raise typer.Exit(2)  # a wrong path, like `scan`, `reason` and `render`
    failed = (
        bool(report.unreadable or report.not_found or report.not_in_archive)
        or report.counts().get("failed", 0) > 0
    )
    raise typer.Exit(1 if failed else 0)


RUN_BUSY = "another worker or run is already running; one at a time: nothing was done"
RUN_LOST = (
    "the run lost its database lock (the connection to Postgres was lost or restarted); it stopped before "
    "the next job and committed nothing, so no worker or other run works beside it. The files it already "
    "changed are not committed (see `git status` in both repos); the jobs it left are still queued. The next "
    "`pipeline.publish` job commits them: `catcher jobs add pipeline.publish`, then run the worker (the next "
    "`run pipeline` publishes too); or commit them by hand"
)
RUN_LOST_AFTER_PUBLISH = (
    "the run lost its database lock after the publish: the publish is done (its commit, and its push with "
    "--push, happened: see the summary); the run stopped there. Run catcher worker --once for any job left"
)
RUN_DB_ERROR = (
    "a database error stopped the run (see the log): documents may already have been moved and may not "
    "be committed; check `git status` in both repos, then run it again"
)
# The statuses of a document whose files the run changed (moved, archived, published or filed).
CHANGED_BY_A_RUN = frozenset({"published", "deferred", "failed", "duplicate", "requeued"})


def _changed_by_the_run(report: RunReport) -> str:
    """How many documents (distinct ids, plus the unreadable files moved to failed/) and artifacts the run
    changed, as `N document(s)` or `N document(s) and M artifact(s)`. A document has several report lines
    when it was requeued and then published: it counts once."""
    documents = {
        item.doc_id
        for item in report.items
        if item.doc_class != "artifact"
        and item.status in CHANGED_BY_A_RUN
        and not (item.status == "failed" and item.message.startswith(NOT_STARTED))
    }
    artifacts = sum(1 for item in report.items if item.doc_class == "artifact" and item.status == "artifact")
    said = f"{len(documents) + len(report.unreadable)} document(s)"
    return f"{said} and {artifacts} artifact(s)" if artifacts else said


def _print_items(report: RunReport) -> None:
    """One line per document of the run, then the problems and the names that were not found."""
    for item in report.items:
        page = f"{item.page} (saved reply)" if item.page and item.llm_saved else item.page or ""
        detail = " ".join(part for part in (page, item.message) if part)
        typer.echo(f"{item.status:<14} {item.doc_class:<15} {item.doc_id:<24} {detail}")
    for problem in report.problems:
        typer.echo(f"{'error':<14} {problem}")
    for rel, error in report.unreadable.items():
        typer.echo(f"{'unreadable':<14} {rel}: {error}")
    for query in report.not_found:
        typer.echo(f'{"not-found":<14} no document named "{query}" in inbox/')
    for query in report.not_in_archive:
        typer.echo(f'{"not-found":<14} no document named "{query}" in archive/')


def _hold_the_run_lock(stack: ExitStack, settings: Settings) -> Callable[[], None]:
    """Take the run lock (`_run_lock`) for as long as `stack` is open and return its check; when a worker or
    another run holds it, or the database cannot be reached, end the command with exit code 2."""
    try:  # released when the stack closes, however the command ends
        return stack.enter_context(_run_lock(settings))
    except WorkerAlreadyRunning as e:
        log.error("%s", RUN_BUSY)
        typer.echo(RUN_BUSY, err=True)
        raise typer.Exit(2) from e
    except OperationalError as e:
        log.error("cannot reach the database in DATABASE_URL: %s", e.orig or e)
        typer.echo("cannot reach the database in DATABASE_URL: nothing was done", err=True)
        raise typer.Exit(2) from e


def _services(stack: ExitStack, settings: Settings) -> Services:
    """The real services; the engine of the Postgres gate they built is disposed when `stack` closes."""
    svc = default_services(settings)
    stack.callback(_dispose_the_gate_engine, svc)
    return svc


def _dispose_the_gate_engine(svc: Services | None) -> None:
    gate = getattr(getattr(svc, "youtube", None), "gate", None)
    if isinstance(gate, PostgresGate):
        gate.engine.dispose()


@contextmanager
def _run_lock(settings: Settings) -> Iterator[Callable[[], None]]:
    """The worker's Postgres advisory lock (`WorkerLock`, the same key) for the whole run; yields its check.

    Raises WorkerAlreadyRunning when a worker or another run holds it, and OperationalError when the
    database cannot be reached."""
    engine = make_worker_engine(settings.database_url)
    try:
        with WorkerLock(engine) as lock:
            yield lock.check
    finally:
        engine.dispose()


RECONCILE_DB_ERROR = "a database error stopped reconcile (see the log): no row was written"


@app.command("reconcile")
def reconcile_cmd(
    ideas: IdeasOpt = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="only say what would change: write nothing, keep the gate")
    ] = False,
    keep_gate: Annotated[
        bool, typer.Option("--keep-gate", help="do not close the YouTube gate (it is closed by default)")
    ] = False,
) -> None:
    """Rebuild the item rows from the idea-bucket folders (after a lost or new database).

    A document in output/, failed/ or duplicates/ without a row gets one, with the status its folder and
    frontmatter give; a row whose file moved gets that status. A row without a file is only reported, never
    deleted, and no file is ever changed. Unless --keep-gate or --dry-run, the YouTube gate is closed for
    YOUTUBE_BLOCK_HOURS (a rebuilt database may have lost a block). It needs the database and takes the run
    lock, like `run pipeline`.

    Exit codes: 0 done; 1 a database error stopped it, or it lost the run lock (no row was written); 2 a
    wrong path, DATABASE_URL is malformed, the database cannot be reached, or a worker or a run is running
    (nothing was done)."""
    settings = Settings()
    _check_database_url(settings.database_url)
    ideas_repo = ideas or settings.ideas_repo
    _check_ideas_inbox(ideas_repo)
    with ExitStack() as stack:
        check = _hold_the_run_lock(stack, settings)
        engine = make_worker_engine(settings.database_url)
        stack.callback(engine.dispose)
        gate_line = None
        gate_note = None  # said when the rows fail after the gate was closed
        if not dry_run and not keep_gate:  # first: a rebuild must never leave the gate open by mistake
            gate = PostgresGate(engine, block_hours=settings.youtube_block_hours, clock=_gate_clock)
            try:
                state = gate.close()
            except GateUnavailable as e:
                _log_gate_unavailable(e)
                typer.echo("could not close the YouTube gate (the database): nothing was done", err=True)
                raise typer.Exit(1) from None
            until = clock_text(state.blocked_until, _gate_clock())
            gate_line = f"YouTube gate closed until {until} (reconcile; use --keep-gate to skip)"
            gate_note = f"the YouTube gate was already closed until {until}; nothing else was changed"
        try:
            with session_scope(engine) as session:
                report = reconcile(
                    session, ideas_repo, now=utc_now(), apply=not dry_run, scan=scan_item_files
                )
                check()  # still ours: commit only while no worker can run beside us
        except WorkerLockLost as e:
            log.error("%s", e)
            typer.echo(f"{e}: no row was written", err=True)
            if gate_note:
                typer.echo(gate_note, err=True)
            raise typer.Exit(1) from e
        except SQLAlchemyError as e:
            log.error("a database error stopped reconcile: %s", getattr(e, "orig", None) or type(e).__name__)
            typer.echo(RECONCILE_DB_ERROR, err=True)
            if gate_note:
                typer.echo(gate_note, err=True)
            raise typer.Exit(1) from e
    for name in report.created:
        typer.echo(f"{'created':<9} {name}")
    for name, old, new in report.status_fixed:
        typer.echo(f"{'fixed':<9} {name}  {old} -> {new}")
    for name in report.missing_files:
        typer.echo(f"{'missing':<9} {name}  no file found; the row is kept")
    for name, why in report.skipped.items():
        typer.echo(f"{'skipped':<9} {name}  {_one_line(why)}")
    typer.echo(
        f"summary: created={len(report.created)} fixed={len(report.status_fixed)} "
        f"missing={len(report.missing_files)} skipped={len(report.skipped)}"
        + (" (dry run: nothing was written)" if dry_run else "")
    )
    if gate_line:
        typer.echo(gate_line)


DEFAULT_CHANNEL_VIDEOS = 50  # --max-videos without a value
# One listing (one gate slot) never asks for more: about 4 paced pages, a bigger channel takes several runs.
# The settings may lower it further (`_check_the_listing_fits_the_gap`).
MAX_CHANNEL_VIDEOS = 100

youtube_app = typer.Typer(no_args_is_help=True, help="YouTube helpers.")
app.add_typer(youtube_app, name="youtube")


@youtube_app.callback()
def youtube_group() -> None:
    """YouTube helpers."""


@youtube_app.command("facts")
def youtube_facts(url: str) -> None:
    """Print the facts (counts, description, transcript) for one video as JSON.

    It goes through the same gap and breaker as a run and the worker (the YouTube gate in DATABASE_URL:
    YOUTUBE_MIN_GAP_S, YOUTUBE_BLOCK_HOURS), and saves nothing. To try it twice in a row, set
    YOUTUBE_MIN_GAP_S=0 for the second call.

    Exit codes: 0 done; 2 the gap or a block stops it, the facts are unavailable, DATABASE_URL is malformed
    or the database cannot be reached (then YouTube is not asked).
    """
    settings = Settings()
    _check_database_url(settings.database_url)
    vid = video_id(url) or url
    access = build_access(settings)
    try:
        facts = access.get(vid, facts_dir=None)
    except FactsUnavailable as e:
        if isinstance(e.__cause__, GateUnavailable):  # the gate's database is down: no call to YouTube
            _log_gate_unavailable(e.__cause__)
            typer.echo(
                "the YouTube gate is unavailable (the database): nothing was asked of YouTube", err=True
            )
        else:
            typer.echo(str(e), err=True)
        raise typer.Exit(2) from None
    finally:
        gate = getattr(access, "gate", None)
        if isinstance(gate, PostgresGate):
            gate.engine.dispose()
    typer.echo(facts.model_dump_json(indent=2))


@contextmanager
def _backfill_session() -> Iterator[Session]:
    """A queue session where a database without tables ends the command with exit code 2."""
    try:
        with _queue_session() as session:
            yield session
    except ProgrammingError as e:
        log.error("the database has no backfill table: %s", e.orig or e)
        typer.echo("the database has no tables yet: run catcher db upgrade (nothing was done)", err=True)
        raise typer.Exit(2) from e


@youtube_app.command("import")
def youtube_import(
    ideas: IdeasOpt = None,
    docs: DocsOpt = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="print the counts, write nothing")] = False,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=0,
            help="then release at most N videos per rolling 24 hours as clip notes into inbox/clippings/",
        ),
    ] = None,
    release_: Annotated[
        bool, typer.Option("--release", help="release with BACKFILL_DAILY_LIMIT as the limit (default 10)")
    ] = False,
    channels: Annotated[
        list[str] | None,
        typer.Option(
            "--channel",
            help="also list this YouTube channel (/@handle, /channel/ID, /c/, /user/) or playlist and add "
            "its videos to the backlog: calls YouTube, one gate slot per channel; try a SMALL channel with a "
            "small --max-videos first. Repeat for more",
        ),
    ] = None,
    max_videos: Annotated[
        int | None,
        typer.Option(
            "--max-videos",
            min=1,
            max=MAX_CHANNEL_VIDEOS,
            help=f"with --channel: list at most N videos per channel (default {DEFAULT_CHANNEL_VIDEOS}, "
            f"at most {MAX_CHANNEL_VIDEOS}: a bigger channel is listed over several runs, known videos are "
            "skipped)",
        ),
    ] = None,
) -> None:
    """Scan the docs for YouTube links that have no page and add them to the backfill backlog.

    A video counts as known when it has a page in the docs, a job item, a clip in the idea bucket or a row
    in the backlog. It takes no worker lock and asks YouTube and the LLM for nothing.

    With --limit N (or --release, which uses BACKFILL_DAILY_LIMIT) it then releases the oldest pending
    videos as clip notes (`inbox/clippings/youtube source - <id>.md`, marked `backfill: true`): the next
    pipeline run makes their pages, at a priority below new clips, through the same YouTube gate. N is a cap
    per rolling 24 hours: running it twice in a day never releases more than N. Start small (5, then 20,
    then 50) and raise YOUTUBE_MIN_GAP_S before raising the limit. The idea bucket is not committed here
    (publish does). With --dry-run it prints what it would release and writes nothing.

    With --channel URL (repeatable) it also lists the videos of a channel or playlist (one flat yt-dlp
    listing of at most --max-videos videos, every request paced by YOUTUBE_REQUEST_DELAY_S) and adds the ones
    not known yet to the backlog (source `channel`). This is the only part that calls YouTube, and it goes
    through the same gate as a fetch: one gate slot per channel, a short gap is waited for (at most
    YOUTUBE_WAIT_MAX_S), a block or a closed gate stops it before any call, and a block during the listing
    opens the breaker as a failed fetch does. No retry. Try the first listing by hand on a SMALL channel
    with a small --max-videos (say 20) and look at `catcher youtube gate` afterwards. --max-videos is at
    most 100 (default 50): one gate slot covers one paced page request per 30 videos, and a listing must end
    well inside the gap its slot started, so it never overlaps a fetch or the next listing: ceil(N / 30) x
    YOUTUBE_REQUEST_DELAY_S must be at most half of YOUTUBE_MIN_GAP_S, else it stops (exit 2) and names the
    largest --max-videos the settings allow. A bigger channel is listed over several runs, on different days,
    and the videos already in the backlog are skipped. The channels of one command are listed one after
    another, each with its own slot, so the gate's gap separates them. With --dry-run nothing is listed and
    no gate slot is taken. YOUTUBE_OFFLINE=1 refuses --channel.

    Exit codes: 0 done; 1 --channel stopped by the gate, a block, YOUTUBE_OFFLINE or a listing error (the
    channels listed before it are kept); 2 a folder is missing, a --channel URL is not a channel or
    playlist, --max-videos does not fit the gap, DATABASE_URL is malformed, the database cannot be reached
    or has no tables (run `catcher db upgrade`)."""
    settings = Settings()
    ideas_repo, docs_repo = ideas or settings.ideas_repo, docs or settings.docs_repo
    for label, folder in (("docs", docs_repo), ("ideas", ideas_repo)):
        if not folder.is_dir():
            typer.echo(f"the {label} folder does not exist: {folder}", err=True)
            raise typer.Exit(2)
    channel_urls = [url.strip() for url in channels or []]
    for url in channel_urls:
        if not backfill_channel.is_channel_url(url):
            typer.echo(f"not a YouTube channel or playlist URL: {url!r} (nothing was done)", err=True)
            raise typer.Exit(2)
    cap = limit if limit is not None else settings.backfill_daily_limit if release_ else None
    if max_videos is not None and not channel_urls:
        typer.echo("--max-videos has no effect without --channel", err=True)
    videos = max_videos if max_videos is not None else DEFAULT_CHANNEL_VIDEOS
    if channel_urls:
        _check_the_listing_fits_the_gap(settings, videos)
    access = None
    if channel_urls and not dry_run:
        _check_database_url(settings.database_url)
        access = build_access(settings, clock=_gate_clock)
    try:
        if access is not None:
            _skip_the_release_on_a_stop(
                cap, lambda: _refuse_a_closed_gate(access)
            )  # before anything is written
        now = utc_now()
        lines: list[str] = []
        with _backfill_session() as session:
            result = run_import(session, ideas_repo, docs_repo, now, dry_run=dry_run)
        typer.echo(
            f"scanned {result.files} file(s), found {result.found} video(s), {result.known} already have a "
            f"page, {result.new} new in the backlog ({result.pending} pending in all)"
        )
        if result.unreadable:
            typer.echo(f"skipped {result.unreadable} unreadable file(s)")
        if channel_urls and dry_run:
            typer.echo(f"would list {len(channel_urls)} channel(s), up to {videos} video(s) each")
        if access is not None:
            _skip_the_release_on_a_stop(
                cap, lambda: _list_channels(access, settings, channel_urls, videos, ideas_repo, docs_repo)
            )
    finally:
        if access is not None and isinstance(access.gate, PostgresGate):
            access.gate.engine.dispose()
    if cap is not None:
        with _backfill_session() as session:
            lines = _release_backlog(session, ideas_repo, cap, utc_now(), result.new_ids if dry_run else None)
    for line in lines:
        typer.echo(line)


def _check_the_listing_fits_the_gap(settings: Settings, videos: int) -> None:
    """Exit 2 (nothing done) when a listing of `videos` could outlast half the gap its gate slot starts: a
    listing longer than the gap would overlap a worker's fetch or the next listing."""
    gap_s, delay_s = settings.youtube_min_gap_s, settings.youtube_request_delay_s
    if backfill_channel.listing_seconds(videos, delay_s) <= gap_s / 2:
        return
    largest = backfill_channel.largest_listing(gap_s / 2, delay_s)
    if largest < 1:
        typer.echo(
            f"no channel listing fits half of YOUTUBE_MIN_GAP_S={gap_s:g} s with YOUTUBE_REQUEST_DELAY_S="
            f"{delay_s:g} s per page: raise the gap (nothing was done)",
            err=True,
        )
    else:
        typer.echo(
            f"--max-videos {videos} takes about {backfill_channel.listing_seconds(videos, delay_s):g} s, "
            f"more than half of YOUTUBE_MIN_GAP_S={gap_s:g} s: use at most --max-videos {largest} with these "
            "settings (nothing was done)",
            err=True,
        )
    raise typer.Exit(2)


def _skip_the_release_on_a_stop(cap: int | None, step: Callable[[], None]) -> None:
    """Run a --channel step; when it stops the command and a release was asked for, say it was skipped."""
    try:
        step()
    except typer.Exit:
        if cap is not None:
            typer.echo(
                "the release (--limit/--release) was skipped: the channel listing stopped; run the command "
                "again later",
                err=True,
            )
        raise


def _refuse_a_closed_gate(access: YoutubeAccess) -> None:
    """--channel: exit 1 before anything is done when YouTube may not be called (offline, a block, a gap
    longer than YOUTUBE_WAIT_MAX_S); exit 2 when the gate cannot answer (its database). Takes no slot."""
    if access.offline:
        typer.echo("YOUTUBE_OFFLINE is on: --channel calls YouTube, nothing was listed", err=True)
        raise typer.Exit(1)
    try:
        wait = access.wait_for_call(wait=True)
    except GateUnavailable as e:
        _log_gate_unavailable(e)
        typer.echo("the YouTube gate is unavailable (the database): nothing was listed", err=True)
        raise typer.Exit(2) from None
    if wait is not None:
        typer.echo(f"{wait.message(access.clock())}: nothing was listed", err=True)
        raise typer.Exit(1)


def _list_channels(
    access: YoutubeAccess, settings: Settings, urls: list[str], max_videos: int, ideas: Path, docs: Path
) -> None:
    """List each channel through the gate (one slot each) and store its unknown videos at once, so a later
    stop keeps what was listed. A stop (the gate, a block, an error) ends the command with exit 1."""
    extractor = backfill_channel.build_extractor(settings.youtube_request_delay_s)
    for url in urls:
        target = backfill_channel.listing_url(url)
        try:
            ids = access.call(
                lambda target=target: backfill_channel.list_channel(
                    target, max_videos=max_videos, extractor=extractor
                ),
                wait=True,
            )
        except FactsDeferred as e:  # the gap, a block (the breaker is now open) or the gate's database
            if isinstance(e.__cause__, GateUnavailable):
                _log_gate_unavailable(e.__cause__)
                typer.echo(f"the YouTube gate is unavailable (the database): {url} was not listed", err=True)
                raise typer.Exit(2) from None
            typer.echo(f"{e}: {url} was not listed (nothing more is listed)", err=True)
            raise typer.Exit(1) from None
        except (FactsUnavailable, backfill_channel.ChannelListingError) as e:
            typer.echo(f"{e} (nothing more is listed)", err=True)
            raise typer.Exit(1) from None
        with _backfill_session() as session:
            known = known_ids(session, ideas, docs)
            fresh = {vid: ("channel", url) for vid in ids if vid not in known}
            added = backfill_store.add_pending(session, fresh, utc_now())
        typer.echo(
            f"listed {len(ids)} video(s) from {url}: {len(ids) - len(fresh)} already known, "
            f"{added} new in the backlog"
        )


def _release_backlog(
    session: Session, ideas: Path, cap: int, now: datetime, dry_new: list[str] | None
) -> list[str]:
    """Release what the rolling 24 h cap allows; the lines to print. `dry_new` (a dry run: the new ids the
    scan would have stored) means nothing is written and the lines say what would be released."""
    allowance = daily_allowance(session, cap, now)
    lines = [] if allowance else [f"daily allowance already used ({cap} per 24 hours)"]
    if dry_new is not None:
        pending = [row.video_id for row in backfill_store.pending(session)] + dry_new
        would = pending[:allowance]
        lines += [f"would release {vid}" for vid in would]
        left = len(pending) - len(would)
        return [*lines, f"would release {len(would)} video(s) into inbox/clippings/ ({left} still pending)"]
    done = release(session, ideas, allowance, now)
    if done.repaired:
        lines.append(f"{len(done.repaired)} video(s) already had a clip note: marked released")
    left = backfill_store.counts(session)["pending"]
    return [*lines, f"released {len(done.released)} video(s) into inbox/clippings/ ({left} still pending)"]


@youtube_app.command("backlog")
def youtube_backlog(
    limit: Annotated[int, typer.Option("--limit", min=1, help="show the oldest N pending videos")] = 20,
) -> None:
    """Show the backfill backlog: the count per status and the oldest pending videos. Read-only.

    Exit codes: 0 done; 2 DATABASE_URL is malformed, the database cannot be reached or has no tables."""
    with _backfill_session() as session:
        counts = backfill_store.counts(session)
        oldest = [(row.video_id, row.found_in) for row in backfill_store.pending(session, limit)]
    typer.echo(f"pending {counts['pending']}, released {counts['released']}")
    for vid, found_in in oldest:
        typer.echo(f"{vid}  {found_in}")


def _log_gate_unavailable(error: GateUnavailable) -> None:
    """Log why the Postgres gate could not answer, without the URL (it holds the password)."""
    cause = error.__cause__
    detail = getattr(cause, "orig", None) or cause or error
    if isinstance(cause, OperationalError):  # logged only, as the other database commands do
        log.error("cannot reach the database in DATABASE_URL: %s", detail)
    else:
        log.error("the YouTube gate in DATABASE_URL is unavailable: %s", detail)


def _gate_clock() -> float:
    return utc_now().timestamp()


def _gate_text(state: GateState, now: float) -> str:
    """The gate as a person reads it, in the words of `Wait.message` (local time, the date when not today)."""
    wait = wait_for(state, now)
    if wait is None:
        return "open"
    until = clock_text(wait.until, now)
    return (
        f"blocked until {until} (block {state.streak})" if wait.blocked else f"next call allowed at {until}"
    )


@youtube_app.command("gate")
def youtube_gate() -> None:
    """Show the YouTube gate (the row `youtube` in DATABASE_URL): open, the gap, or a block.

    Exit codes: 0 done; 2 DATABASE_URL is malformed or the database cannot be reached."""
    settings = Settings()
    _check_database_url(settings.database_url)
    engine = make_worker_engine(settings.database_url)
    gate = PostgresGate(engine, block_hours=settings.youtube_block_hours, clock=_gate_clock)
    try:
        state = gate.snapshot()
        typer.echo(f"youtube: {_gate_text(state, _gate_clock())}")
    except GateUnavailable as e:
        _log_gate_unavailable(e)
        raise typer.Exit(2) from None
    finally:
        engine.dispose()


db_app = typer.Typer(no_args_is_help=True, help="The database schema (Alembic migrations).")
app.add_typer(db_app, name="db")


@db_app.callback()
def db_group() -> None:
    """The database schema (Alembic migrations)."""


@db_app.command("upgrade")
def db_upgrade(revision: Annotated[str, typer.Argument()] = "head") -> None:
    """Upgrade the database in DATABASE_URL to REVISION (default: the latest)."""
    alembic_command.upgrade(alembic_config(Settings().database_url), revision)


@db_app.command("downgrade", context_settings={"ignore_unknown_options": True})  # so that -1 is a revision
def db_downgrade(
    revision: Annotated[
        str, typer.Argument(help="a revision, or -1 for one step back; base drops everything")
    ],
    yes: Annotated[bool, typer.Option("--yes", help="do not ask before downgrading to base")] = False,
) -> None:
    """Downgrade the database in DATABASE_URL to REVISION.

    `base` drops every table with all its rows, so it asks first unless --yes is given."""
    if revision == "base" and not yes:
        typer.confirm("Downgrade to base drops every table and all its rows. Continue?", abort=True)
    alembic_command.downgrade(alembic_config(Settings().database_url), revision)


def _positive_seconds(option: str, value: float) -> float:
    if not (math.isfinite(value) and value > 0):
        raise typer.BadParameter(f"must be a number of seconds above 0, not {value}", param_hint=option)
    return value


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@contextmanager
def _stop_on_signals(stop: threading.Event) -> Iterator[None]:
    """SIGTERM and SIGINT set `stop`: the worker finishes the job it runs, then exits. A second signal
    raises KeyboardInterrupt at once (the job stays running; the reaper requeues it after its lease).
    Signal handlers can only be installed from the main thread; elsewhere nothing is installed. The
    previous handlers come back when the block ends."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handle(signum: int, frame: object) -> None:
        if stop.is_set():
            raise KeyboardInterrupt
        log.info("signal %s: the worker stops after the current job", signal.Signals(signum).name)
        stop.set()

    previous = {signum: signal.signal(signum, handle) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


SCHEDULER_JOIN_S = 30.0  # a tick waits at most 3 x the 10 s lock timeout; then the worker leaves it


def _next_slot(spec: ScheduleSpec, tz: ZoneInfo, now: datetime) -> datetime:
    """The next slot after `now`, as a wall time in `tz` (the same mapping `due_slot` uses)."""
    wall = croniter(spec.cron, now.astimezone(tz).replace(tzinfo=None)).get_next(datetime)
    return wall.replace(tzinfo=tz, fold=0)


def _next_text(spec: ScheduleSpec, tz: ZoneInfo, now: datetime) -> str:
    return f"next {_next_slot(spec, tz, now):%H:%M}"


@app.command()
def worker(
    once: Annotated[
        bool, typer.Option("--once", help="run the jobs that are due now, then exit (no waiting)")
    ] = False,
    ideas: IdeasOpt = None,
    docs: DocsOpt = None,
    lease_s: Annotated[
        float, typer.Option("--lease-s", help="seconds a claimed job is ours without a heartbeat")
    ] = 120.0,
    poll_s: Annotated[float, typer.Option("--poll-s", help="seconds to wait when no job is due")] = 2.0,
) -> None:
    """Run the jobs in the queue, one at a time, until stopped (Ctrl-C or SIGTERM ends it after the
    current job). Only one worker runs at a time: a second one exits with code 2.

    Exit codes: 0 stopped normally (also when jobs failed: see `catcher jobs list`); 1 the worker lost
    its one-worker lock, or with --once a claim hit a database error or a job could not be finished;
    2 another worker or a `run pipeline` runs, DATABASE_URL is malformed or the database cannot be reached."""
    lease_s = _positive_seconds("--lease-s", lease_s)
    poll_s = _positive_seconds("--poll-s", poll_s)
    settings = Settings()
    _check_database_url(settings.database_url)
    specs: list[ScheduleSpec] = []
    tz = None
    if not once:  # a bad cron or timezone ends the command before the lock is tried
        try:
            tz = load_timezone(settings)
            specs = parse_schedules(settings)
        except ValueError as e:
            log.error("%s", e)
            typer.echo(str(e), err=True)
            raise typer.Exit(2) from None
    ctx = build_context(settings, ideas=ideas, docs=docs)
    stop = threading.Event()
    failed_once = False
    scheduler_thread: threading.Thread | None = None
    try:
        with WorkerLock(ctx.engine) as lock, _stop_on_signals(stop):
            runner = Worker(
                ctx,
                build_handlers(),
                worker_id=_worker_id(),
                lease_s=lease_s,
                heartbeat_s=lease_s / 3,
                poll_s=poll_s,
                lock_check=lock.check,
            )
            log.info("worker %s started (once=%s)", runner.worker_id, once)
            if once:
                counts: Counter[str] = Counter()
                runner.check_lock()
                runner.reap_safely()
                while not stop.is_set():
                    outcome = runner.run_once()
                    if outcome is None:
                        break
                    if outcome == "claim_error":  # not idle: the queue could not be read
                        typer.echo("could not claim a job: a database error (see the log)", err=True)
                        failed_once = True
                        break
                    counts[outcome] += 1
                summary = " ".join(f"{name}={n}" for name, n in sorted(counts.items()))
                typer.echo(f"ran {counts.total()} job(s){': ' + summary if summary else ''}")
                if counts["error"]:
                    typer.echo(
                        f"{counts['error']} job(s) could not be finished (see the log); the reaper "
                        "requeues them when their lease expires",
                        err=True,
                    )
                    failed_once = True
            else:
                if specs and tz is not None:
                    # ctx.engine is the worker engine (lock_timeout), so a stuck row lock cannot hang a tick
                    for spec in specs:
                        log.info(
                            "schedule %s: %r %s, %s",
                            spec.name,
                            spec.cron,
                            tz.key,
                            _next_text(spec, tz, utc_now()),
                        )
                    scheduler = Scheduler(ctx.engine, specs, tz, lambda: utc_now())
                    scheduler_thread = threading.Thread(
                        target=scheduler.run_forever,
                        args=(stop, settings.schedule_tick_s),
                        name="scheduler",
                    )
                    scheduler_thread.start()
                try:
                    runner.run_forever(stop)
                finally:
                    stop.set()  # also when the worker ends on its own (lock lost): the scheduler stops too
                    if scheduler_thread is not None:
                        scheduler_thread.join(SCHEDULER_JOIN_S)
                        if scheduler_thread.is_alive():
                            log.error(
                                "the scheduler thread did not stop within %s s; leaving it", SCHEDULER_JOIN_S
                            )
            log.info("worker %s stopped", runner.worker_id)
    except WorkerAlreadyRunning as e:
        log.error("%s", e)
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    except WorkerLockLost as e:
        log.error("%s", e)
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    except OperationalError as e:
        log.error("cannot reach the database in DATABASE_URL: %s", e.orig or e)
        raise typer.Exit(2) from e
    finally:
        ctx.engine.dispose()
    if failed_once:
        raise typer.Exit(1)


@app.command()
def health() -> None:
    """Tell whether a worker holds the one-worker lock (for a Docker healthcheck). No lock, no writes.

    Exit codes: 0 a worker is running; 1 no worker holds the lock; 2 DATABASE_URL is malformed or the
    database cannot be reached."""
    url = Settings().database_url
    _check_database_url(url)
    engine = create_engine(url, connect_args={"connect_timeout": 5})  # fast: a healthcheck runs every 30 s
    try:
        running = worker_running(engine)
    except OperationalError as e:
        log.error("cannot reach the database in DATABASE_URL: %s", e.orig or e)
        raise typer.Exit(2) from e
    finally:
        engine.dispose()
    if not running:
        typer.echo("no worker holds the lock")
        raise typer.Exit(1)
    typer.echo("worker running")


@app.command("api")
def api_command(
    host: Annotated[
        str, typer.Option("--host", help="address to listen on (0.0.0.0 for the LAN)")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535, help="port to listen on")] = 8000,
) -> None:
    """Serve the HTTP API (Swagger UI at /docs). Needs API_KEYS and DATABASE_URL.

    Exit code 2 before it listens when API_KEYS is missing or not valid, or DATABASE_URL is not set or
    malformed."""
    import uvicorn  # here, not at the top: the other commands do not pay for loading the web stack

    from catcher.api.app import create_app
    from catcher.api.auth import parse_api_keys

    settings = Settings()
    try:
        parse_api_keys(settings.api_keys.get_secret_value())
    except ValueError as e:  # the message names the problem and the entry, never the key
        typer.echo(f"{e}; the format is name:scope[,scope]:key, entries separated by spaces", err=True)
        raise typer.Exit(2) from None
    if not os.environ.get("DATABASE_URL"):
        typer.echo(
            "DATABASE_URL is not set: catcher api does not run on the built-in development database; "
            "set DATABASE_URL (in the environment or .env)",
            err=True,
        )
        raise typer.Exit(2)
    _check_database_url(settings.database_url)
    server_app = create_app(settings)
    # log_config=None: uvicorn leaves logging alone; its own records (start, stop, errors) go through the
    # project's handlers, format and level instead.
    share_project_handlers("uvicorn")
    logging.getLogger("catcher.api").info("listening on %s:%s", host, port)
    # access_log=False on purpose: an access line holds the full query string, and the plan wants nothing
    # a client sends to end up in the logs.
    uvicorn.run(server_app, host=host, port=port, log_config=None, access_log=False)


jobs_app = typer.Typer(no_args_is_help=True, help="The job queue: add jobs by hand and look at them.")
app.add_typer(jobs_app, name="jobs")


@jobs_app.callback()
def jobs_group() -> None:
    """The job queue: add jobs by hand and look at them."""


_INT = re.compile(r"[+-]?\d+")


def _param_value(raw: str) -> bool | int | str:
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    return int(raw) if _INT.fullmatch(raw) else raw


LIST_PARAMS = ("only", "requeue")  # always a list of names; a repeated --param adds one


def _parse_params(pairs: list[str]) -> dict[str, Any]:
    """`KEY=VALUE` pairs to job params. `only` and `requeue` always become a list of the names as typed
    (never split on commas: a file name can hold one), and repeating them adds names. Any other key is
    given once; true/false become booleans and whole numbers integers."""
    params: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise typer.BadParameter(f"{pair!r} is not KEY=VALUE", param_hint="--param")
        if re.sub(r"[-_\s]", "", key.lower()) == "dryrun":
            typer.echo(
                "dry runs never go through the queue: use `catcher run pipeline --dry-run` instead", err=True
            )
            raise typer.Exit(2)
        if key in LIST_PARAMS:
            params.setdefault(key, []).append(raw)
            continue
        if key in params:
            raise typer.BadParameter(f"{key!r} is given twice", param_hint="--param")
        params[key] = _param_value(raw)
    return params


def _check_database_url(url: str) -> None:
    """Exit 2 with a short message when DATABASE_URL cannot be parsed (or names a driver that is not
    installed). The message never shows the URL: it holds the password."""
    try:
        make_url(url).get_dialect()
    except (ArgumentError, ValueError, ImportError) as e:
        log.error("DATABASE_URL is not a valid database URL (%s)", type(e).__name__)
        typer.echo(
            "DATABASE_URL is not a valid database URL: use postgresql+psycopg://USER:PASSWORD@HOST:PORT/DB",
            err=True,
        )
        raise typer.Exit(2) from None


@contextmanager
def _queue_session() -> Iterator[Session]:
    """A session on DATABASE_URL; a malformed URL or a database that cannot be reached ends the command
    with exit code 2."""
    url = Settings().database_url
    _check_database_url(url)
    engine = make_worker_engine(url)
    try:
        with session_scope(engine) as session:
            yield session
    except OperationalError as e:
        log.error("cannot reach the database in DATABASE_URL: %s", e.orig or e)
        raise typer.Exit(2) from e
    finally:
        engine.dispose()


@jobs_app.command("add")
def jobs_add(
    job_type: Annotated[
        str,
        typer.Argument(metavar="TYPE", help="the job type, e.g. pipeline.run, pipeline.publish, ideas.pull"),
    ],
    param: Annotated[
        list[str] | None,
        typer.Option(
            "--param",
            help="KEY=VALUE for the job (true/false become booleans, whole numbers integers). "
            "only=NAME and requeue=NAME are lists: repeat them for more names. Repeat for more",
        ),
    ] = None,
    priority: Annotated[int, typer.Option("--priority", help="higher runs first")] = 0,
) -> None:
    """Put one job on the queue, due now, and print its id and the version. The type must have a handler
    and the params must pass that handler's own check, or nothing is queued (exit 2). Dry runs are refused:
    they never go through the queue."""
    params = _parse_params(param or [])
    try:
        check_job(job_type, params)
    except ValueError as e:
        typer.echo(f"cannot queue {job_type}: {e}", err=True)
        raise typer.Exit(2) from None
    with _queue_session() as session:
        job, _ = queue.enqueue(
            session,
            type=job_type,
            now=utc_now(),
            priority=priority,
            params=params,
            resource=JOB_RESOURCES.get(job_type),
        )
        job_id = job.id
    typer.echo(f"job queued, version {__version__}, job id {job_id}")


@jobs_app.command("list")
def jobs_list(
    status: Annotated[
        str | None, typer.Option("--status", help=f"only jobs with this status: {', '.join(JOB_STATUSES)}")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="show at most N jobs (newest first)")] = 50,
) -> None:
    """List jobs, newest first: id, type, status, priority, run_after, reason (or error). A queued job whose
    resource is closed (a fetch job while the YouTube gate waits) shows until when it waits."""
    if status is not None and status not in JOB_STATUSES:
        raise typer.BadParameter(f"must be one of {', '.join(JOB_STATUSES)}", param_hint="--status")
    now = utc_now()
    statement = (
        select(Job, queue.resource_closed_until(now)).order_by(Job.created_at.desc(), Job.id).limit(limit)
    )
    if status is not None:
        statement = statement.where(Job.status == status)
    with _queue_session() as session:
        rows = [(job, closed_until) for job, closed_until in session.execute(statement)]
    for job, closed_until in rows:
        run_after = job.run_after.isoformat(timespec="seconds")
        reason = job.error or job.reason or ""
        if job.status == "queued" and closed_until is not None and not job.error:  # skipped until then
            until = clock_text(closed_until.timestamp(), now.timestamp())
            reason = f"waiting for {job.resource} until {until}"
        typer.echo(
            f"{job.id}  {job.type:<16} {job.status:<9} {job.priority:>4}  {run_after}  {reason}".rstrip()
        )


@app.command()
def schedules() -> None:
    """Show the three schedules of `catcher worker`: name, cron, timezone, last fired (UTC) and next due
    (in the schedule timezone). An empty variable shows `off`. Read-only; the worker need not run."""
    settings = Settings()
    try:
        tz = load_timezone(settings)
        specs = {spec.name: spec for spec in parse_schedules(settings)}
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from None
    now = utc_now()
    with _queue_session() as session:
        fired = {row.name: row.last_fired_at for row in session.scalars(select(Schedule))}
    rows = [("name", "cron", "timezone", "last fired", "next due")]
    for name in SCHEDULE_NAMES:
        spec = specs.get(name)
        if spec is None:
            rows.append((name, "off", "-", "-", "-"))
            continue
        last = fired.get(name)
        last_text = f"{last:%Y-%m-%d %H:%M}" if last else "never"
        if last is not None and due_slot(spec, tz, last, now) is not None:
            next_text = "due now"
        else:
            next_text = f"{_next_slot(spec, tz, now):%Y-%m-%d %H:%M}"
        rows.append((name, spec.cron, tz.key, last_text, next_text))
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    for row in rows:
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())


items_app = typer.Typer(no_args_is_help=True, help="The documents the worker handles: their state.")
app.add_typer(items_app, name="items")
ITEM_REASON_CHARS = 80


@items_app.callback()
def items_group() -> None:
    """The documents the worker handles (one row per document in the database): their state."""


def _one_line(text: str, width: int = ITEM_REASON_CHARS) -> str:
    """`text` on one line (whitespace runs become one space), cut to `width` characters with `...`."""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 3].rstrip() + "..."


@items_app.command("list")
def items_list(
    status: Annotated[
        str | None,
        typer.Option("--status", help=f"only items with this status: {', '.join(ITEM_STATUSES)}"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="show at most N items (newest first)")] = 50,
) -> None:
    """List the items, newest first (by when they entered their status): name, class, status, since
    (local time) and the reason (or the error), shortened to one line. Read-only. A stuck item shows here."""
    if status is not None and status not in ITEM_STATUSES:
        raise typer.BadParameter(f"must be one of {', '.join(ITEM_STATUSES)}", param_hint="--status")
    since = func.coalesce(JobItem.stage_since, JobItem.updated_at)
    statement = select(JobItem).order_by(since.desc(), JobItem.calculated_name).limit(limit)
    if status is not None:
        statement = statement.where(JobItem.status == status)
    with _queue_session() as session:
        rows = [
            (
                item.calculated_name,
                item.doc_class,
                item.status,
                item.stage_since or item.updated_at,
                item.stage_reason or item.error or "",
            )
            for item in session.scalars(statement)
        ]
    for name, doc_class, item_status, at, why in rows:
        when = at.astimezone().strftime("%Y-%m-%d %H:%M")
        typer.echo(f"{name}  {doc_class:<8} {item_status:<15} {when}  {_one_line(why)}".rstrip())


testdata_app = typer.Typer(no_args_is_help=True, help="Test data for trying the Idea Catcher on copies.")
app.add_typer(testdata_app, name="testdata")


@testdata_app.callback()
def testdata_group() -> None:
    """Test data for trying the Idea Catcher on copies."""


@testdata_app.command("reset")
def testdata_reset(
    target: Annotated[Path, typer.Option("--target", help="where the test repos are made")] = DEFAULT_TARGET,
    source: Annotated[Path, typer.Option("--source", help="the committed test data")] = DEFAULT_SOURCE,
    fresh_llm_and_youtube: Annotated[
        bool,
        typer.Option(
            "--fresh-llm-and-youtube",
            help="leave out the saved LLM replies (llm/) and YouTube facts (facts/): a run then calls the "
            "LLM and YouTube for real (it costs money, the YouTube gap applies)",
        ),
    ] = False,
) -> None:
    """Delete the test repos and make fresh ones (idea-bucket, epiaku-docs) from the committed test data."""
    try:
        repos = reset_test_repos(target, source, fresh_llm_and_youtube=fresh_llm_and_youtube)
    except TestDataError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    for path in repos.values():
        typer.echo(f"made {path}")
    if fresh_llm_and_youtube:
        typer.echo(
            "idea-bucket has no saved LLM replies or YouTube facts: a run calls the LLM and YouTube for real"
        )
    ideas, docs = (os.path.relpath(repos[name]) for name in ("idea-bucket", "epiaku-docs"))
    typer.echo(f"run on them: uv run catcher run pipeline --ideas {ideas} --docs {docs} --profile fake")
