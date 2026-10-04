import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from catcher import __version__
from catcher.core.files import file_lock
from catcher.core.git import GitError, commit_paths, pull, push
from catcher.modules.llm.trace import LLM_DIR, TraceStore
from catcher.modules.pipeline.inbox import (
    Note,
    Requeued,
    ScanResult,
    copy_artifact,
    deferred_in_output,
    mark_deferred,
    move_to_duplicates,
    move_to_failed,
    note_label,
    requeue_from_archive,
    return_to_inbox,
    scan_inbox,
    start_work,
)
from catcher.modules.pipeline.outcome import Outcome, classify
from catcher.modules.pipeline.process import (
    ProcessedPage,
    ProcessOptions,
    Services,
    process_note,
    reject_invalid_page,
)
from catcher.modules.pipeline.publish import write_output, write_page
from catcher.modules.pipeline.steps import order_notes, split_duplicates
from catcher.modules.youtube.cache import FACTS_DIR, FactsCache
from catcher.modules.youtube.urls import video_id

log = logging.getLogger("catcher.run")

Status = Literal[
    "published",
    "would_publish",
    "deferred",
    "failed",
    "skipped",
    "duplicate",
    "artifact",
    "would_copy",
    "requeued",
    "would_requeue",
    "waiting",
    "would_fetch",
    "interrupted",
]


@dataclass
class ItemReport:
    doc_id: str
    doc_class: str
    status: Status
    message: str = ""
    page: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    llm_saved: bool = False  # the page was made from a saved LLM reply: the tokens are recorded, not spent


@dataclass
class RunOptions:
    profile: str | None = None
    dry_run: bool = False
    push: bool = False
    limit: int | None = None
    only: list[str] | None = None  # process only the documents with these names
    requeue: list[str] | None = (
        None  # first copy these documents from archive/ back into inbox/, then run them
    )
    refresh_facts: bool = False  # fetch the YouTube facts again even when they are saved
    wait_youtube: bool = False  # sleep through a short gap between YouTube calls instead of waiting
    retry_deferred: bool = False  # first put the documents a temporary error stalled back into inbox/
    refresh_llm: bool = False  # call the LLM even when a good reply is saved in llm/ (`--requeue` reuses it)


@dataclass
class RunReport:
    items: list[ItemReport] = field(default_factory=list)
    unreadable: dict[str, str] = field(default_factory=dict)  # files that could not be read, now in failed/
    not_found: list[str] = field(default_factory=list)  # `--file` names that matched no inbox document
    problems: list[str] = field(default_factory=list)  # a setup problem that stopped the run (a wrong path)
    not_in_archive: list[str] = field(
        default_factory=list
    )  # `--requeue` names that matched no archive document
    committed: dict[str, bool] = field(default_factory=dict)
    pushed: bool = False

    def counts(self) -> dict[str, int]:
        return dict(Counter(item.status for item in self.items))


@dataclass
class RunState:
    """What the run loop remembers from one document to the next."""

    blocked: set[str]  # backends not to call again in this run (a usage limit or budget was hit)
    budget_blocked: dict[str, int]  # backend -> notes waiting because its budget is used up
    attempted: int  # documents worked on, counted against --limit
    seen_ids: set[str]  # ids processed so far, to warn when a page replaces an earlier one


def outcome_message(outcome: Outcome, state: RunState) -> str:
    """The report message: a usage limit on a backend whose budget is already used up is the budget too.
    `classify` cannot know that (it is stateless), so the rule lives here."""
    backend = outcome.backend
    if backend is not None and not outcome.budget and backend in state.budget_blocked:
        return f"budget reached ({backend})"
    return outcome.message


def log_outcome(who: str, outcome: Outcome, message: str, error: BaseException) -> None:
    """One log line per outcome, at the level the run has always used. Call it inside the `except` block,
    so `log.exception` has the traceback."""
    if outcome.unexpected:
        log.exception("%s: failed, %s", who, message)
    elif outcome.kind == "failed":
        log.error("%s: failed, %s", who, message)
    elif outcome.config_error:
        log.error("%s: deferred, configuration error: %s", who, message)
    elif outcome.budget:
        log.warning("%s: deferred, %s: %s", who, message, error)
    elif outcome.kind == "deferred":
        log.warning("%s: deferred, %s", who, message)
    elif outcome.kind == "interrupted":
        log.warning("%s: interrupted, back in inbox/", who)
    elif outcome.kind == "waiting":
        log.info("%s: waiting, %s", who, message)
    else:  # would_fetch
        log.info("%s: %s", who, message)


def apply_outcome(
    ideas: Path,
    note: Note,
    outcome: Outcome,
    state: RunState,
    item: ItemReport,
    *,
    dry_run: bool,
    now: datetime | None = None,
) -> list[Path]:
    """A document did not get published: record why in its report item and in the run state, and put the
    document where it belongs (a dry run moves nothing). Returns the touched idea-bucket paths."""
    message = outcome_message(outcome, state)
    item.status, item.message = outcome.kind, message
    backend = outcome.backend
    if backend is not None:
        state.blocked.add(backend)
        if outcome.budget or backend in state.budget_blocked:
            state.budget_blocked[backend] = state.budget_blocked.get(backend, 0) + 1
    if outcome.kind == "waiting":
        state.attempted -= 1  # it was not worked on, so it does not count against --limit
    if dry_run:
        return []
    if outcome.kind == "deferred":  # a temporary error: the working copy stays in output/ and says why
        return mark_deferred(ideas, note, message, now)
    if outcome.kind == "failed":
        return move_to_failed(
            ideas, note.output_path(ideas), message, doc_id=note.doc_id, doc_class=note.doctype.name, now=now
        )
    if outcome.kind in ("waiting", "interrupted"):  # leave it where the next run finds it
        return return_to_inbox(ideas, note)
    return []  # would_fetch: a dry run only


def finish(ideas: Path, note: Note, processed: ProcessedPage) -> list[Path]:
    """The document is ready: the working copy in `output/` becomes the final page."""
    return write_output(ideas, note, processed.page)


def copy_artifacts(
    scan: ScanResult,
    ideas: Path,
    docs: Path,
    opts: RunOptions,
    svc: Services,
    report: RunReport,
    touched_ideas: list[Path],
    touched_docs: list[Path],
) -> None:
    """Files in `inbox/` that are not markdown: rename, archive, copy to epiaku-docs. No LLM, so `--limit`
    does not apply."""
    limit_mb = svc.settings.artifact_max_mb
    for artifact in scan.artifacts:
        item = ItemReport(artifact.original_name, "artifact", "skipped")
        report.items.append(item)
        size_mb = artifact.size / (1024 * 1024)
        if size_mb > limit_mb:
            item.message = f"{size_mb:.1f} MB is over the {limit_mb} MB limit (ARTIFACT_MAX_MB)"
            log.warning(
                'artifact "%s": skipped, %s; it stays in inbox/', artifact.original_name, item.message
            )
            continue
        if opts.dry_run:
            item.status, item.message = (
                "would_copy",
                "to archive/artifacts/ and epiaku-docs idea-bucket/artifacts/",
            )
            log.info(
                'artifact "%s": would copy to archive/artifacts/ and epiaku-docs', artifact.original_name
            )
            continue
        try:
            ideas_paths, docs_paths = copy_artifact(ideas, docs, artifact)
        except OSError as e:
            item.status, item.message = "failed", f"could not copy: {e}"
            log.error('artifact "%s": %s; it stays in inbox/', artifact.original_name, item.message)
            continue
        touched_ideas += ideas_paths
        touched_docs += docs_paths
        item.status, item.page = "artifact", artifact.name


def run_pipeline(ideas: Path, docs: Path, opts: RunOptions, svc: Services) -> RunReport:
    """One run over the inbox. Only one run at a time per machine: a second one is refused, because both
    would pick up the same documents. A dry run changes nothing, so it needs no lock."""
    if opts.dry_run:
        return _run(ideas, docs, opts, svc)
    lock_file = svc.settings.catcher_state_dir.expanduser() / "pipeline.lock"
    with file_lock(lock_file, blocking=False) as held:
        if not held:
            message = f"another catcher run is in progress on this machine ({lock_file}): nothing was done"
            log.error(message)
            return RunReport(problems=[message])
        return _run(ideas, docs, opts, svc)


def _run(ideas: Path, docs: Path, opts: RunOptions, svc: Services) -> RunReport:
    report = RunReport()
    log.info(
        "run started: version=%s ideas=%s docs=%s profile=%s dry_run=%s push=%s limit=%s",
        __version__,
        ideas,
        docs,
        opts.profile or "class default",
        opts.dry_run,
        opts.push,
        opts.limit,
    )
    for what, folder, needed in (
        ("idea-bucket inbox/", ideas / "inbox", True),
        ("epiaku-docs", docs, not opts.dry_run),
    ):
        if needed and not folder.is_dir():
            message = f"{what} not found at {folder}: check --ideas/--docs or IDEAS_REPO/DOCS_REPO"
            log.error(message)
            report.problems.append(message)
    if report.problems:
        return report  # nothing was touched
    if opts.push:
        try:
            pull(ideas)
            pull(docs)
        except GitError as e:
            report.problems.append(f"{e}: nothing was changed")
            return report

    touched_ideas: list[Path] = []
    touched_docs: list[Path] = []
    facts_dir = ideas / FACTS_DIR  # the saved YouTube facts, one file per video
    llm_dir = ideas / LLM_DIR  # the LLM traces, one file per processed document
    requeued: list[Requeued] = []
    explicit = opts.requeue or []
    stalled = deferred_in_output(ideas) if opts.retry_deferred else []
    if explicit or stalled:
        requeued, not_found = requeue_from_archive(ideas, [*explicit, *stalled], dry_run=opts.dry_run)
        report.not_in_archive = [q for q in not_found if q in explicit]
        for query in report.not_in_archive:
            log.warning('no document named "%s" found in archive/: nothing to requeue', query)
        for item in requeued:
            status: Status = "requeued" if item.copied else "would_requeue" if opts.dry_run else "skipped"
            touched_ideas += item.touched
            message = f"archive/{item.rel.as_posix()} -> inbox/"
            if status == "skipped":
                message = f"inbox/{item.rel.as_posix()} already exists, not overwritten"
            report.items.append(ItemReport(item.doc_id, item.doc_class, status, message))
    # `--requeue` runs only the requeued documents, like `--file` does for the ones it names
    only = None if opts.only is None and not explicit else [*(opts.only or []), *explicit]
    scan = scan_inbox(ideas, only=only)
    for rel, reason in scan.errors.items():  # unreadable: archive it and move it to failed/
        report.unreadable[rel] = reason
        if not opts.dry_run:
            touched_ideas += move_to_failed(ideas, ideas / rel, f"cannot read the capture: {reason}")
    ordered = order_notes(scan.notes)

    for query in opts.only or []:
        if query not in scan.matched:
            report.not_found.append(query)
            log.warning(
                'no document named "%s" found in inbox/. Only inbox/ is searched: '
                "to run a document from archive/ again, use --requeue; from failed/ or duplicates/, "
                "move it into inbox/",
                query,
            )

    # Several clips of one conversation: only the longest goes to the LLM.
    # Earlier snapshots of it move to duplicates/ (nothing is deleted).
    to_process, duplicates = split_duplicates(ordered)
    for note, winner in duplicates:
        item = ItemReport(
            note.doc_id, note.doctype.name, "duplicate", f"duplicate of {winner.rel.as_posix()}"
        )
        report.items.append(item)
        if not opts.dry_run:
            touched_ideas.extend(move_to_duplicates(ideas, note, winner))
        log.info(
            "%s: %s, an earlier clip of the same conversation as %s (no LLM call)",
            note_label(note),
            "would move to duplicates/" if opts.dry_run else "moved to duplicates/",
            note_label(winner),
        )
    ordered = to_process

    total = len(ordered)
    log.info(
        "%d note(s) to process (%d in the inbox, %d unreadable and moved to failed/)",
        total,
        len(scan.notes),
        len(scan.errors),
    )

    state = RunState(blocked=set(), budget_blocked={}, attempted=0, seen_ids=set())
    left_by_limit = 0
    for position, note in enumerate(ordered, start=1):
        who = f"({position}/{total}) {note_label(note)}"
        item = ItemReport(note.doc_id, note.doctype.name, "skipped")
        report.items.append(item)
        if opts.limit is not None and state.attempted >= opts.limit:
            item.message = "run limit reached"
            left_by_limit += 1  # one line for all of them after the loop: an inbox can hold hundreds
            continue
        vid = video_id(str(note.doc.fm.get("source") or "")) if note.doctype.name == "youtube" else None
        if vid and svc.youtube is not None:
            # A clip that must wait for YouTube stays in inbox/ untouched: the next run picks it up.
            waiting = svc.youtube.wait_needed(
                vid, facts_dir=facts_dir, refresh=opts.refresh_facts, wait=opts.wait_youtube
            )
            if waiting is not None:
                item.status, item.message = "waiting", f"{waiting.message()} (stays in inbox/)"
                log.info("%s: waiting, %s", who, item.message)
                continue
        state.attempted += 1
        log.info("%s: processing", who)
        if note.doc_id in state.seen_ids:
            item.message = "same id as an earlier document in this run: this page replaces the earlier one"
            log.warning("%s: %s", who, item.message)
        state.seen_ids.add(note.doc_id)
        popts = ProcessOptions(
            profile=opts.profile,
            dry_run=opts.dry_run,
            blocked_backends=frozenset(state.blocked),
            facts_dir=facts_dir,
            refresh_facts=opts.refresh_facts,
            wait_youtube=opts.wait_youtube,
            llm_dir=llm_dir,
            refresh_llm=opts.refresh_llm,
        )
        try:
            if not opts.dry_run:  # out of inbox/: archive + working copy in output/
                try:
                    touched_ideas += start_work(ideas, note)
                except OSError as e:  # the document is still in inbox/ (or comes back in the handler)
                    touched_ideas += return_to_inbox(ideas, note)
                    item.status, item.message = "failed", f"could not start work: {e}"
                    log.error("%s: %s", who, item.message)
                    continue
            processed = process_note(note, svc, popts)
        except (Exception, KeyboardInterrupt) as e:  # one broken note must not stop the run; Ctrl-C does
            outcome = classify(e)
            # Back to inbox/ (waiting, interrupted): the file moves, then the log line; else the reverse.
            back_to_inbox = outcome.kind in ("waiting", "interrupted")
            if not back_to_inbox:
                log_outcome(who, outcome, outcome_message(outcome, state), e)
            touched_ideas += apply_outcome(ideas, note, outcome, state, item, dry_run=opts.dry_run)
            if back_to_inbox:
                log_outcome(who, outcome, item.message, e)
            if outcome.kind == "interrupted":  # Ctrl-C or SIGTERM: the rest waits for the next run
                report.problems.append("interrupted: the remaining documents were not processed")
                break
            continue
        finally:
            if vid and not opts.dry_run:  # the saved facts are committed, whatever happened next
                saved = FactsCache(facts_dir).path(vid)
                if saved.exists():
                    touched_ideas.append(saved)
            if not opts.dry_run:  # the LLM trace too, also when the call failed (same name as output/)
                trace = TraceStore(llm_dir).path_for(note.target_rel)
                if trace.is_file():
                    touched_ideas.append(trace)

        item.tokens_in, item.tokens_out = processed.llm.usage.tokens_in, processed.llm.usage.tokens_out
        item.page = processed.filename
        item.llm_saved = processed.llm.from_saved
        if processed.problems:
            item.status, item.message = "failed", "; ".join(processed.problems)
            log.error("%s: failed, page is invalid: %s", who, item.message)
            if not opts.dry_run:
                touched_ideas += reject_invalid_page(
                    ideas, llm_dir, note, processed, svc, f"page is invalid: {item.message}"
                )
        elif opts.dry_run:
            item.status = "would_publish"
            log.info("%s: would publish %s", who, processed.filename)
        else:
            touched_docs += write_page(
                docs, note.destination, note.doc_id, processed.filename, processed.page
            )
            touched_ideas += finish(ideas, note, processed)
            item.status = "published"
            log.info("%s: published %s", who, processed.filename)
        if processed.dropped_tags:
            dropped = f"dropped tags: {', '.join(processed.dropped_tags)}"
            item.message = f"{item.message}; {dropped}" if item.message else dropped
            log.warning("%s: %s", who, dropped)

    for backend, waiting in state.budget_blocked.items():
        log.error(
            "%s budget reached: %d note(s) waiting; "
            "raise the key's budget or point the profile at another provider",
            backend,
            waiting,
        )
    if left_by_limit:
        log.info("limit of %d reached: %d note(s) stay in inbox/", opts.limit, left_by_limit)
    copy_artifacts(scan, ideas, docs, opts, svc, report, touched_ideas, touched_docs)
    counts = report.counts()
    log.info(
        "processed %d/%d: %s",
        state.attempted,
        total,
        ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "nothing to do",
    )
    if not opts.dry_run:
        author = (svc.settings.git_author_name, svc.settings.git_author_email)
        published = report.counts().get("published", 0)
        artifacts = report.counts().get("artifact", 0)
        docs_message = f"idea-catcher: publish {published} page(s)"
        if artifacts:
            docs_message += f" and {artifacts} artifact(s)"
        try:
            report.committed["docs"] = commit_paths(docs, touched_docs, docs_message, author=author)
            report.committed["ideas"] = commit_paths(
                ideas,
                touched_ideas,
                f"idea-catcher: process the inbox ({published} published)",
                author=author,
            )
            if opts.push:
                push(docs)
                push(ideas)
                report.pushed = True
        except GitError as e:  # the files are written; only the git step failed
            report.problems.append(f"{e}: the changes are in the files but not (fully) committed or pushed")
    log.info("run finished: %s committed=%s pushed=%s", report.counts(), report.committed, report.pushed)
    return report
