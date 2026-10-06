"""`catcher run pipeline --dry-run`: what a run would do with the inbox, and nothing else.

Read-only. It scans `inbox/`, asks the saved YouTube facts (`facts/`) and the saved LLM replies (`llm/`)
what they already hold, and builds each page in memory. It writes no file, moves no file, writes no row and
queues no job, never touches the YouTube gate (a document without saved facts is `would_fetch`: it does not
even ask the gate), and calls neither a model nor YouTube (a document without a saved reply is
`would_call_llm`). The report has the shape and the line wording of a real run (`_print_items` prints it)."""

import logging
from pathlib import Path
from typing import Any

from catcher import __version__
from catcher.modules.llm.profiles import Profile, resolve_profile
from catcher.modules.llm.trace import LLM_DIR
from catcher.modules.pipeline.inbox import (
    Note,
    deferred_in_output,
    note_label,
    requeue_from_archive,
    scan_inbox,
)
from catcher.modules.pipeline.outcome import classify
from catcher.modules.pipeline.process import (
    ProcessOptions,
    Services,
    ask_llm,
    build_page,
    get_facts,
)
from catcher.modules.pipeline.report import LIMIT_REACHED, SAME_ID, ItemReport, RunReport
from catcher.modules.pipeline.steps import order_notes, split_duplicates
from catcher.modules.youtube.cache import FACTS_DIR

log = logging.getLogger("catcher.run")


class _WouldCallTheModel(Exception):
    """Raised in place of the model call: no saved reply holds this document's answer."""


def _stop_before_the_call(profile: Profile) -> None:
    raise _WouldCallTheModel


def preview(ideas: Path, docs: Path, params: dict[str, Any], services: Services) -> RunReport:
    """The report of a run that would start now with `params` (the `pipeline.run` params: `profile`, `limit`,
    `only`, `requeue`, `retry_deferred`, `refresh_llm`, `refresh_facts`). `docs` is not read: a preview does
    not need the docs folder."""
    report = RunReport()
    limit: int | None = params.get("limit")
    log.info(
        "run started: version=%s ideas=%s docs=%s profile=%s dry_run=%s push=%s limit=%s",
        __version__,
        ideas,
        docs,
        params.get("profile") or "class default",
        True,
        False,
        limit,
    )
    if not (ideas / "inbox").is_dir():
        message = (
            f"idea-bucket inbox/ not found at {ideas / 'inbox'}: check --ideas/--docs or IDEAS_REPO/DOCS_REPO"
        )
        log.error(message)
        report.problems.append(message)
        return report  # nothing was touched

    explicit: list[str] = list(params.get("requeue") or [])
    stalled = deferred_in_output(ideas) if params.get("retry_deferred") else []
    if explicit or stalled:
        requeued, not_found = requeue_from_archive(ideas, [*explicit, *stalled], dry_run=True)
        report.not_in_archive = [q for q in not_found if q in explicit]
        for query in report.not_in_archive:
            log.warning('no document named "%s" found in archive/: nothing to requeue', query)
        for entry in requeued:
            message = f"archive/{entry.rel.as_posix()} -> inbox/"
            report.items.append(ItemReport(entry.doc_id, entry.doc_class, "would_requeue", message))

    only: list[str] = list(params.get("only") or [])
    # `--requeue` runs only the requeued documents, like `--file` does for the ones it names
    scan = scan_inbox(ideas, only=None if not only and not explicit else [*only, *explicit])
    report.unreadable.update(scan.errors)
    for query in only:
        if query not in scan.matched:
            report.not_found.append(query)
            log.warning(
                'no document named "%s" found in inbox/. Only inbox/ is searched: '
                "to run a document from archive/ again, use --requeue; from failed/ or duplicates/, "
                "move it into inbox/",
                query,
            )

    to_process, duplicates = split_duplicates(order_notes(scan.notes))
    for note, winner in duplicates:
        message = f"duplicate of {winner.rel.as_posix()}"
        report.items.append(ItemReport(note.doc_id, note.doctype.name, "duplicate", message))
        log.info(
            "%s: would move to duplicates/, an earlier clip of the same conversation as %s (no LLM call)",
            note_label(note),
            note_label(winner),
        )

    total = len(to_process)
    log.info(
        "%d note(s) to process (%d in the inbox, %d unreadable and moved to failed/)",
        total,
        len(scan.notes),
        len(scan.errors),
    )
    attempted = 0
    seen_ids: set[str] = set()
    for position, note in enumerate(to_process, start=1):
        who = f"({position}/{total}) {note_label(note)}"
        item = ItemReport(note.doc_id, note.doctype.name, "skipped")
        report.items.append(item)
        if limit is not None and attempted >= limit:
            item.message = LIMIT_REACHED
            continue
        attempted += 1
        if note.doc_id in seen_ids:
            item.message = SAME_ID
            log.warning("%s: %s", who, item.message)
        seen_ids.add(note.doc_id)
        _classify(ideas, note, services, params, item, who)

    counts = report.counts()
    log.info(
        "processed %d/%d: %s",
        attempted,
        total,
        ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "nothing to do",
    )
    max_mb = services.settings.artifact_max_mb
    for artifact in scan.artifacts:
        entry = ItemReport(artifact.original_name, "artifact", "skipped")
        report.items.append(entry)
        size_mb = artifact.size / (1024 * 1024)
        if size_mb > max_mb:
            entry.message = f"{size_mb:.1f} MB is over the {max_mb} MB limit (ARTIFACT_MAX_MB)"
            log.warning(
                'artifact "%s": skipped, %s; it stays in inbox/', artifact.original_name, entry.message
            )
            continue
        entry.status = "would_copy"
        entry.message = "to archive/artifacts/ and epiaku-docs idea-bucket/artifacts/"
        log.info('artifact "%s": would copy to archive/artifacts/ and epiaku-docs', artifact.original_name)
    log.info("run finished: %s committed=%s pushed=%s", report.counts(), report.committed, report.pushed)
    return report


def _classify(
    ideas: Path, note: Note, svc: Services, params: dict[str, Any], item: ItemReport, who: str
) -> None:
    """Fill the report line of one document: the page it would make, or what stops it. Reads saved facts and
    saved replies only."""
    opts = ProcessOptions(
        profile=params.get("profile"),
        dry_run=True,
        facts_dir=ideas / FACTS_DIR,
        refresh_facts=bool(params.get("refresh_facts")),
        llm_dir=ideas / LLM_DIR,
        refresh_llm=bool(params.get("refresh_llm")),
        allow_fetch=False,  # saved facts only: YouTube and its gate are never asked
    )
    try:
        profile_name, _ = resolve_profile(
            svc.profiles, requested=opts.profile, class_default=note.doctype.llm_profile
        )
        facts = get_facts(note, svc, opts)
        result = ask_llm(
            note,
            svc,
            profile_name,
            facts,
            llm_dir=opts.llm_dir,
            dry_run=True,
            refresh_llm=opts.refresh_llm,
            before_call=_stop_before_the_call,
        )
        processed = build_page(note, svc, result, facts)
    except _WouldCallTheModel:
        item.status, item.message = "would_call_llm", "no saved reply: a run would ask the model"
        log.info("%s: %s", who, item.message)
        return
    except Exception as e:  # one broken note must not stop the preview
        outcome = classify(e)
        item.status, item.message = outcome.kind, outcome.message
        log.info("%s: %s, %s", who, outcome.kind, outcome.message)
        return
    item.tokens_in, item.tokens_out = processed.llm.usage.tokens_in, processed.llm.usage.tokens_out
    item.page = processed.filename
    item.llm_saved = processed.llm.from_saved
    if processed.problems:
        item.status, item.message = "failed", "; ".join(processed.problems)
        log.error("%s: failed, page is invalid: %s", who, item.message)
    else:
        item.status = "would_publish"
        log.info("%s: would publish %s", who, processed.filename)
    if processed.dropped_tags:
        dropped = f"dropped tags: {', '.join(processed.dropped_tags)}"
        item.message = f"{item.message}; {dropped}" if item.message else dropped
        log.warning("%s: %s", who, dropped)
