import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from catcher import __version__
from catcher.core.files import file_lock
from catcher.core.git import GitError, commit_paths, pull, push
from catcher.modules.llm.profiles import UnknownProfile
from catcher.modules.llm.service import (
    BackendUnavailable,
    BudgetExhausted,
    InputRejected,
    InvalidOutput,
    UsageLimitReached,
)
from catcher.modules.pipeline.inbox import (
    Note,
    Requeued,
    ScanResult,
    copy_artifact,
    deferred_in_output,
    is_snapshot_of,
    mark_deferred,
    move_to_duplicates,
    move_to_failed,
    note_label,
    requeue_from_archive,
    return_to_inbox,
    scan_inbox,
    start_work,
)
from catcher.modules.pipeline.process import ProcessedPage, ProcessOptions, Services, process_note
from catcher.modules.pipeline.publish import write_output, write_page
from catcher.modules.youtube.cache import FACTS_DIR, FactsCache
from catcher.modules.youtube.facts import FactsDeferred, FactsUnavailable, FetchSkipped
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
    ordered = sorted(
        scan.notes, key=lambda n: (str(n.doc.fm.get("captured", "")), n.doc_id, n.path.as_posix())
    )

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
    winners: dict[str, Note] = {}
    for note in ordered:
        best = winners.get(note.doc_id)
        if best is None or len(note.doc.body) > len(best.doc.body):
            winners[note.doc_id] = note
    to_process: list[Note] = []
    for note in ordered:
        winner = winners[note.doc_id]
        if note is winner or not is_snapshot_of(note.doc.body, winner.doc.body):
            to_process.append(note)
            continue
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

    def file_as_failed(note: Note, reason: str) -> None:
        if not opts.dry_run:
            touched_ideas.extend(
                move_to_failed(
                    ideas, note.output_path(ideas), reason, doc_id=note.doc_id, doc_class=note.doctype.name
                )
            )

    def stall(note: Note, reason: str) -> None:
        """A temporary error: the working copy stays in output/ and says why."""
        if not opts.dry_run:
            touched_ideas.extend(mark_deferred(ideas, note, reason))

    blocked: set[str] = set()
    budget_blocked: dict[str, int] = {}  # backend -> notes waiting because its budget is used up
    attempted = 0
    seen_ids: set[str] = set()
    for position, note in enumerate(ordered, start=1):
        who = f"({position}/{total}) {note_label(note)}"
        item = ItemReport(note.doc_id, note.doctype.name, "skipped")
        report.items.append(item)
        if opts.limit is not None and attempted >= opts.limit:
            item.message = "run limit reached"
            log.info("%s: skipped, %s", who, item.message)
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
        attempted += 1
        log.info("%s: processing", who)
        if note.doc_id in seen_ids:
            item.message = "same id as an earlier document in this run: this page replaces the earlier one"
            log.warning("%s: %s", who, item.message)
        seen_ids.add(note.doc_id)
        popts = ProcessOptions(
            profile=opts.profile,
            dry_run=opts.dry_run,
            blocked_backends=frozenset(blocked),
            facts_dir=facts_dir,
            refresh_facts=opts.refresh_facts,
            wait_youtube=opts.wait_youtube,
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
        except KeyboardInterrupt:  # Ctrl-C or SIGTERM: leave the document where the next run finds it
            if not opts.dry_run:
                touched_ideas += return_to_inbox(ideas, note)
            item.status, item.message = "interrupted", "back in inbox/"
            log.warning("%s: interrupted, back in inbox/", who)
            report.problems.append("interrupted: the remaining documents were not processed")
            break
        except BudgetExhausted as e:
            blocked.add(e.backend)
            budget_blocked[e.backend] = budget_blocked.get(e.backend, 0) + 1
            item.status, item.message = "deferred", f"budget reached ({e.backend})"
            log.warning("%s: deferred, %s: %s", who, item.message, e)
            stall(note, item.message)
            continue
        except UsageLimitReached as e:
            blocked.add(e.backend)
            if e.backend in budget_blocked:
                budget_blocked[e.backend] += 1
                item.status, item.message = "deferred", f"budget reached ({e.backend})"
            else:
                item.status, item.message = "deferred", f"usage limit ({e.backend}): {e}"
            log.warning("%s: deferred, %s", who, item.message)
            stall(note, item.message)
            continue
        except BackendUnavailable as e:
            item.status, item.message = "deferred", str(e)
            log.warning("%s: deferred, %s", who, item.message)
            stall(note, item.message)
            continue
        except UnknownProfile as e:  # a configuration problem, not a bad note
            item.status, item.message = "deferred", str(e)
            log.error("%s: deferred, configuration error: %s", who, item.message)
            stall(note, item.message)
            continue
        except (InvalidOutput, InputRejected) as e:  # the document itself: a retry would fail the same way
            item.status, item.message = "failed", str(e)
            log.error("%s: failed, %s", who, item.message)
            file_as_failed(note, item.message)
            continue
        except FactsDeferred as e:  # YouTube is closed for now (the gap, the breaker): wait in inbox/
            if not opts.dry_run:
                touched_ideas += return_to_inbox(ideas, note)
            attempted -= 1  # it was not worked on, so it does not count against --limit
            item.status, item.message = "waiting", f"{e} (back in inbox/)"
            log.info("%s: waiting, %s", who, item.message)
            continue
        except FetchSkipped as e:  # a dry run does not call YouTube
            item.status, item.message = "would_fetch", str(e)
            log.info("%s: %s", who, item.message)
            continue
        except FactsUnavailable as e:
            item.status, item.message = "deferred", str(e)
            log.warning("%s: deferred, %s", who, item.message)
            stall(note, item.message)
            continue
        except Exception as e:  # one broken note must not stop the run
            item.status, item.message = "failed", f"unexpected {type(e).__name__}: {e}"
            log.exception("%s: failed, %s", who, item.message)
            file_as_failed(note, item.message)
            continue
        finally:
            if vid and not opts.dry_run:  # the saved facts are committed, whatever happened next
                saved = FactsCache(facts_dir).path(vid)
                if saved.exists():
                    touched_ideas.append(saved)

        item.tokens_in, item.tokens_out = processed.llm.usage.tokens_in, processed.llm.usage.tokens_out
        item.page = processed.filename
        if processed.problems:
            item.status, item.message = "failed", "; ".join(processed.problems)
            log.error("%s: failed, page is invalid: %s", who, item.message)
            file_as_failed(note, f"page is invalid: {item.message}")
        elif opts.dry_run:
            item.status = "would_publish"
            log.info("%s: would publish %s", who, processed.filename)
        else:
            touched_docs += write_page(docs, note.doctype, note.doc_id, processed.filename, processed.page)
            touched_ideas += finish(ideas, note, processed)
            item.status = "published"
            log.info("%s: published %s", who, processed.filename)
        if processed.dropped_tags:
            dropped = f"dropped tags: {', '.join(processed.dropped_tags)}"
            item.message = f"{item.message}; {dropped}" if item.message else dropped
            log.warning("%s: %s", who, dropped)

    for backend, waiting in budget_blocked.items():
        log.error(
            "%s budget reached: %d note(s) waiting; "
            "raise the key's budget or point the profile at another provider",
            backend,
            waiting,
        )
    copy_artifacts(scan, ideas, docs, opts, svc, report, touched_ideas, touched_docs)
    counts = report.counts()
    log.info(
        "processed %d/%d: %s",
        attempted,
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
