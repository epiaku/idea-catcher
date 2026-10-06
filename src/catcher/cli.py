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
from pathlib import Path
from typing import Annotated, Any

import typer
from alembic import command as alembic_command
from dotenv import load_dotenv
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from catcher import __version__
from catcher.core.config import Settings
from catcher.core.db import alembic_config, make_worker_engine, session_scope, utc_now
from catcher.core.log import configure_logging
from catcher.core.testdata import DEFAULT_SOURCE, DEFAULT_TARGET, TestDataError, reset_test_repos
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
from catcher.modules.pipeline.process import ProcessOptions, Services, default_services, process_note
from catcher.modules.pipeline.publish import write_page
from catcher.modules.pipeline.run import NOT_STARTED, RunLockLost, RunOptions, RunReport, run_pipeline
from catcher.modules.pipeline.scan_state import scan_item_files
from catcher.modules.pipeline.tags import load_tags
from catcher.modules.queue import queue
from catcher.modules.queue.models import ITEM_STATUSES, JOB_STATUSES, Job, JobItem
from catcher.modules.queue.reconcile import reconcile
from catcher.modules.worker.app import JOB_RESOURCES, build_context, build_handlers, check_job
from catcher.modules.worker.guard import WorkerAlreadyRunning, WorkerLock, WorkerLockLost
from catcher.modules.worker.loop import Worker
from catcher.modules.youtube.access import build_access
from catcher.modules.youtube.cache import FACTS_DIR
from catcher.modules.youtube.facts import FactsUnavailable
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
            help="when a clip must wait for the gap between YouTube calls, sleep (up to YOUTUBE_WAIT_MAX_S) "
            "instead of leaving it in inbox/ for a later run",
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

    It needs the database: first it takes the worker's lock in DATABASE_URL (also for --dry-run), so one
    worker or run works at a time, and holds it until the run ends.

    Exit codes: 0 done; 1 a document failed or a name was not found, or the run lost its database lock (it
    stopped before the next document and committed nothing); 2 a wrong path, DATABASE_URL is malformed, the
    database cannot be reached, or a worker or another run is running (nothing was done)."""
    settings = Settings()
    signal.signal(signal.SIGTERM, _terminate)  # a `kill` ends the run like Ctrl-C: the document goes back
    opts = RunOptions(
        profile=profile,
        dry_run=dry_run,
        push=push,
        limit=limit,
        only=file,
        requeue=requeue,
        refresh_facts=refresh_facts,
        wait_youtube=wait_youtube,
        retry_deferred=retry_deferred,
        refresh_llm=refresh_llm,
    )
    _check_database_url(settings.database_url)
    with ExitStack() as stack:
        opts.lock_check = _hold_the_run_lock(stack, settings)  # before any file is touched
        try:
            report = run_pipeline(
                ideas or settings.ideas_repo, docs or settings.docs_repo, opts, _services(stack, settings)
            )
        except RunLockLost as e:
            _print_items(e.report)
            typer.echo(f"{_changed_by_the_run(e.report)} were finished and are NOT committed", err=True)
            log.error("%s", RUN_LOST)
            typer.echo(RUN_LOST, err=True)
            raise typer.Exit(1) from e
        except SQLAlchemyError as e:  # the lock's own errors are handled above; this one came from the run
            log.error("a database error stopped the run: %s", getattr(e, "orig", None) or type(e).__name__)
            typer.echo(RUN_DB_ERROR, err=True)
            raise typer.Exit(1) from e
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
    "the next step and committed nothing, so no worker or other run works beside it. The files it already "
    "changed are not committed (see `git status` in both repos), and the next `run pipeline` does not commit "
    "them either: it commits only the files it changes itself. They are committed by the next "
    "`pipeline.publish` job: `catcher jobs add pipeline.publish`, then run the worker; or commit them by hand"
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
    ctx = build_context(settings, ideas=ideas, docs=docs)
    stop = threading.Event()
    failed_once = False
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
                runner.run_forever(stop)
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
    job_type: Annotated[str, typer.Argument(metavar="TYPE", help="the job type, e.g. pipeline.run")],
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
