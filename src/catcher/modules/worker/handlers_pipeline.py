"""The `pipeline.run`, `llm.reason`, `youtube.fetch` and `pipeline.publish` handlers.

`pipeline.run` stages the inbox documents, database row first, then the file move.

Per note: (1) its calculated name and a `staging` item row, committed; (2) `start_work` (archive copy, working
copy in `output/`, out of `inbox/`); (3) the item waits for YouTube or the LLM and its next job is queued, in
one commit. A crash between those steps leaves a `staging` row, which the next run adopts under the same
name. The handler runs no git command (publishing is `pipeline.publish`) and holds no long transaction.

`llm.reason` turns one staged working copy in `output/` into its page: saved facts only (it never calls
YouTube), the saved LLM reply before the model, then the file effects of Stage A, then the item status (and
a new fetch job) in one commit. The model call runs outside any transaction. It is safe to run twice: a
committed outcome is not redone, and a final page already in `output/` (a crash before the commit) is only
recorded as published; a crash before `finish` reruns from the saved reply, so the model is paid once.

`youtube.fetch` gets the facts of one staged clip and queues its `llm.reason`. The YouTube call runs with
no session open and never sleeps: a closed gate (the gap, or the breaker after a 429) defers the job to the
gate's time (no attempt counted) and the item keeps waiting. No facts to be had (no transcript, a gone video,
a yt-dlp failure) defers the item, as in Stage A, and the job succeeds. Saved facts mean no second call.

`pipeline.publish` is the only handler that runs git. Per repo (the idea-bucket, then epiaku-docs with the
pages and `idea-bucket/artifacts`): commit the managed folders, pull with a rebase, push. Committing first
means the pull's autostash never holds the worker's own changes. Changes outside those folders are left
alone. A failed rebase is aborted and fails the job; the commit stays local, and the next publish pulls and
pushes it."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from catcher.core.db import session_scope
from catcher.core.frontmatter import FrontmatterError
from catcher.core.git import GIT_LOCK, GitError, ahead_of_upstream, commit_managed, has_remote, pull, push
from catcher.modules.llm.trace import LLM_DIR
from catcher.modules.pipeline.doctypes import DOC_TYPES
from catcher.modules.pipeline.inbox import (
    ARTIFACTS_DIR,
    STAGE_ANALYZED,
    STAGE_DEFERRED,
    Note,
    assign_name,
    deferred_in_output,
    load_staged_note,
    move_to_duplicates,
    move_to_failed,
    note_label,
    requeue_from_archive,
    return_to_inbox,
    scan_inbox,
    start_work,
    with_filename_fields,
)
from catcher.modules.pipeline.outcome import classify
from catcher.modules.pipeline.process import ProcessOptions, get_facts, process_note, reject_invalid_page
from catcher.modules.pipeline.publish import write_page
from catcher.modules.pipeline.run import (
    ItemReport,
    RunOptions,
    RunReport,
    RunState,
    apply_outcome,
    copy_artifacts,
    finish,
    log_outcome,
)
from catcher.modules.pipeline.steps import order_notes, split_duplicates
from catcher.modules.queue.items import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    ItemExists,
    get_item,
    items_in_status,
    set_item_status,
    stage_item,
)
from catcher.modules.queue.models import Job
from catcher.modules.queue.queue import enqueue
from catcher.modules.worker.handlers import Defer, Done, Fail, HandlerContext, HandlerResult
from catcher.modules.youtube.cache import FACTS_DIR, FactsCache
from catcher.modules.youtube.facts import FactsDeferred
from catcher.modules.youtube.urls import video_id

log = logging.getLogger("catcher.worker.pipeline")

NO_DOCUMENT = "staging row without a document"
DEFER_FALLBACK_S = 600  # a FactsDeferred without a time (a gate that does not say): ask again in 10 minutes


@dataclass(frozen=True)
class RunParams:
    only: list[str] | None = None
    requeue: list[str] | None = None
    retry_deferred: bool = False
    limit: int | None = None
    profile: str | None = None
    refresh_llm: bool = False


def parse_params(params: dict[str, Any]) -> RunParams:
    """The job's params, checked. Raises ValueError with a message that names the bad parameter."""
    unknown = sorted(set(params) - set(RunParams.__dataclass_fields__))
    if unknown:
        raise ValueError(f"unknown parameter(s) for pipeline.run: {', '.join(unknown)}")
    for key in ("only", "requeue"):
        value = params.get(key)
        if value is not None and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise ValueError(f"{key} must be a list of names, not {value!r}")
    for key in ("retry_deferred", "refresh_llm"):
        if not isinstance(params.get(key, False), bool):
            raise ValueError(f"{key} must be true or false, not {params[key]!r}")
    limit = params.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError(f"limit must be a whole number of 0 or more, not {limit!r}")
    profile = params.get("profile")
    if profile is not None and not (isinstance(profile, str) and profile.strip()):
        raise ValueError(f"profile must be a profile name, not {profile!r}")
    return RunParams(**params)


def _facts_saved(ctx: HandlerContext, vid: str) -> bool:
    """True when `facts/<vid>.json` holds usable facts (the same check the fetch makes). Never fetches."""
    facts_dir = ctx.ideas / FACTS_DIR
    youtube = ctx.services.youtube
    cache = youtube.cache(facts_dir) if youtube is not None else FactsCache(facts_dir)
    return cache is not None and cache.get(vid) is not None


def _queue_next(ctx: HandlerContext, name: str, note: Note, params: RunParams) -> None:
    """Step 3: the item waits for YouTube (a clip without saved facts) or for the LLM, and its next job is
    queued, in one commit. The dedupe key makes a second call a no-op while that job is active. Both jobs
    carry `profile` and `refresh_llm` when set: the fetch handler passes them on to its `llm.reason` job."""
    vid = video_id(str(note.doc.fm.get("source") or "")) if note.doctype.name == "youtube" else None
    fetch = vid is not None and not _facts_saved(ctx, vid)  # file IO before the transaction
    job_params: dict[str, Any] = {"calculated_name": name}
    if params.profile is not None:
        job_params["profile"] = params.profile
    if params.refresh_llm:
        job_params["refresh_llm"] = True
    status, job_type, key = (
        ("waiting_youtube", "youtube.fetch", f"fetch:{name}")
        if fetch
        else ("waiting_llm", "llm.reason", f"reason:{name}")
    )
    now = ctx.clock()
    with session_scope(ctx.engine) as session:
        set_item_status(session, name, status, now=now)
        enqueue(session, type=job_type, now=now, params=job_params, dedupe_key=key)


def _inbox_note(ideas: Path, inbox_path: str, now: datetime) -> Note | None:
    """The note at `inbox_path` (`inbox/<sub>/<file>`), read as a scan reads it, or None."""
    parts = Path(inbox_path).parts
    if len(parts) < 2 or parts[0] != "inbox" or ".." in parts:
        return None
    rel = Path(*parts[1:])
    if not (ideas / "inbox" / rel).is_file():
        return None
    scan = scan_inbox(ideas, now=now, only=[rel.as_posix()])
    return next((n for n in scan.notes if n.inbox_rel == rel), None)


def _same_document(ideas: Path, name: str, note: Note) -> bool:
    """True when the inbox file is the document archived as `archive/<name>`: its bytes plus the two file
    name fields, exactly what `start_work` archived. False for another capture that landed at that path."""
    try:
        raw = note.path.read_bytes().decode("utf-8")
        archived = (ideas / "archive" / name).read_bytes()
    except (OSError, UnicodeDecodeError):
        return False
    return archived == with_filename_fields(raw, note.original_name, Path(name).name).encode("utf-8")


def _start(ctx: HandlerContext, name: str, note: Note) -> bool:
    """Step 2, with Stage A's handling of a file error: the document goes back to `inbox/` (`return_to_inbox`,
    under its calculated name), the item is `failed` with the reason, and the run goes on. A later run stages
    it again under the same name (the failed row is reset). False when it failed."""
    try:
        start_work(ctx.ideas, note, ctx.clock())
        return True
    except OSError as e:
        message = f"could not start work: {e}"
    log.error("%s: %s", note_label(note), message)
    try:
        return_to_inbox(ctx.ideas, note)
    except OSError as e:
        log.error("%s: could not put it back in inbox/ either: %s", note_label(note), e)
    with session_scope(ctx.engine) as session:
        set_item_status(session, name, "failed", now=ctx.clock(), reason=message)
    return False


@dataclass(frozen=True)
class Leftover:
    """A `staging` row: a crash came between its commit and its step 3."""

    name: str
    doc_id: str
    inbox_path: str | None


def _adopt(ctx: HandlerContext, row: Leftover, params: RunParams) -> tuple[str, Path | None]:
    """Finish what a crash left of one `staging` row: "adopted", "failed" or "error", and the inbox path used.

    The inbox file is used when `output/<name>` is missing (the move did not finish or never began), or when
    it is the very document archived under the name (the crash came between the output copy and the
    unlink): `start_work` under the same name and with the row's id overwrites the copies and takes it out of
    `inbox/`. Another document at that path is left for the scan, and the working copy in `output/` goes on
    to step 3. Neither file: the item is failed."""
    name, now = row.name, ctx.clock()
    out = ctx.ideas / "output" / name
    note = _inbox_note(ctx.ideas, row.inbox_path, now) if row.inbox_path else None
    if note is not None:
        note.name = Path(name).name
        if note.target_rel.as_posix() == name and (
            not out.is_file() or _same_document(ctx.ideas, name, note)
        ):
            note.doc_id = note.doc.fm["id"] = row.doc_id  # a class without a derivable id got a new one
            if not _start(ctx, name, note):
                return "error", note.inbox_rel
            _queue_next(ctx, name, note, params)
            log.info("adopted %s: started again from inbox/", name)
            return "adopted", note.inbox_rel
    if out.is_file():
        try:
            staged = load_staged_note(ctx.ideas, out, now)
        except (FrontmatterError, UnicodeDecodeError, ValueError) as e:
            log.error("adopting %s: cannot read output/%s: %s", name, name, e)
        else:
            _queue_next(ctx, name, staged, params)
            log.info("adopted %s: its working copy was already in output/", name)
            return "adopted", None
    with session_scope(ctx.engine) as session:
        set_item_status(session, name, "failed", now=ctx.clock(), reason=NO_DOCUMENT)
    log.error("item %s: %s; marked failed", name, NO_DOCUMENT)
    return "failed", None


def _requeue(ctx: HandlerContext, queries: list[str]) -> None:
    """Move the named archived documents back into `inbox/` (Stage A's requeue), except those whose item is
    still active: their files are in use by a queued job and stay where they are."""
    if not queries:
        return
    found, not_found = requeue_from_archive(ctx.ideas, queries, dry_run=True)
    for query in not_found:
        log.warning('no document named "%s" found in archive/: nothing to requeue', query)
    keep: list[str] = []
    with session_scope(ctx.engine) as session:
        for item in found:
            rel = item.rel.as_posix()
            row = get_item(session, rel)
            if row is not None and row.status in ACTIVE_STATUSES:
                log.warning("not requeued: %s is still being processed (status %s)", rel, row.status)
            else:
                keep.append(rel)
    if keep:
        requeue_from_archive(ctx.ideas, keep)


def handle_pipeline_run(ctx: HandlerContext, job: Job) -> HandlerResult:
    """Stage the inbox: adopt crash leftovers, requeue, then stage every note and queue its next job."""
    try:
        params = parse_params(dict(job.params or {}))
    except ValueError as e:
        return Fail(str(e))
    ideas = ctx.ideas
    for what, folder in (("idea-bucket inbox/", ideas / "inbox"), ("epiaku-docs", ctx.docs)):
        if not folder.is_dir():
            return Fail(f"{what} not found at {folder}")
    counts = {"staged": 0, "adopted": 0, "duplicates": 0, "artifacts": 0, "unreadable": 0, "errors": 0}

    with session_scope(ctx.engine) as session:  # a single worker: every staging row is a crash leftover
        leftovers = [
            Leftover(i.calculated_name, i.doc_id, i.inbox_path) for i in items_in_status(session, "staging")
        ]
    tried: set[Path] = set()  # inbox files adoption already worked on: the scan leaves them alone this run
    for row in leftovers:
        try:
            outcome, used = _adopt(ctx, row, params)
        except OSError as e:  # one document's file problem must not stop the run; the row stays staging
            log.error("adopting %s: %s; trying again next run", row.name, e)
            outcome, used = "error", None
        if used is not None:
            tried.add(used)
        if outcome == "adopted":
            counts["adopted"] += 1
        elif outcome == "error":
            counts["errors"] += 1

    explicit = params.requeue or []
    _requeue(ctx, [*explicit, *(deferred_in_output(ideas) if params.retry_deferred else [])])

    # `requeue` runs only the requeued documents, like `only` does for the ones it names (as in Stage A)
    only = None if params.only is None and not explicit else [*(params.only or []), *explicit]
    now = ctx.clock()
    scan = scan_inbox(ideas, now=now, only=only)
    for rel, reason in scan.errors.items():
        move_to_failed(ideas, ideas / rel, f"cannot read the capture: {reason}", now=now)
        counts["unreadable"] += 1
    to_process, duplicates = split_duplicates(order_notes(scan.notes))
    for note, winner in duplicates:
        move_to_duplicates(ideas, note, winner)
        counts["duplicates"] += 1
        log.info("%s: moved to duplicates/, an earlier clip of %s", note_label(note), note_label(winner))

    for note in to_process:
        if note.inbox_rel in tried:
            continue
        if params.limit is not None and counts["staged"] >= params.limit:
            log.info("%s: run limit reached, stays in inbox/", note_label(note))
            continue
        name = _stage(ctx, job, note)
        if name is None:
            continue
        if not _start(ctx, name, note):
            counts["errors"] += 1
            continue
        _queue_next(ctx, name, note, params)
        counts["staged"] += 1

    report = RunReport()
    copy_artifacts(scan, ideas, ctx.docs, RunOptions(), ctx.services, report, [], [])
    counts["artifacts"] = report.counts().get("artifact", 0)
    log.info("pipeline.run: %s", counts)
    return Done(counts)


def _stage(ctx: HandlerContext, job: Job, note: Note) -> str | None:
    """Step 1: the calculated name and the `staging` row, committed before any file moves. None when the
    item is already active (a capture that came back while its item is still being processed)."""
    assign_name(ctx.ideas, note)
    name = note.target_rel.as_posix()
    try:
        with session_scope(ctx.engine) as session:
            stage_item(
                session,
                calculated_name=name,
                doc_id=note.doc_id,
                doc_class=note.doctype.name,
                now=ctx.clock(),
                inbox_path=f"inbox/{note.rel.as_posix()}",
                original_filename=note.original_name,
                root_job_id=job.id,
            )
    except ItemExists as e:
        log.warning("%s: %s; it stays in inbox/", note_label(note), e)
        return None
    return name


@dataclass(frozen=True)
class ReasonParams:
    calculated_name: str
    profile: str | None = None
    refresh_llm: bool = False


def parse_reason_params(params: dict[str, Any], job_type: str = "llm.reason") -> ReasonParams:
    """The params of an `llm.reason` or `youtube.fetch` job (the same three), checked. Raises ValueError
    with a message that names the bad one."""
    unknown = sorted(set(params) - set(ReasonParams.__dataclass_fields__))
    if unknown:
        raise ValueError(f"unknown parameter(s) for {job_type}: {', '.join(unknown)}")
    name = params.get("calculated_name")
    path = Path(name) if isinstance(name, str) else None
    plain = path is not None and not path.is_absolute() and ".." not in path.parts
    if path is None or not plain or len(path.parts) != 2 or path.suffix != ".md":
        raise ValueError(f"calculated_name must be <subfolder>/<name>.md, not {name!r}")
    if not isinstance(params.get("refresh_llm", False), bool):
        raise ValueError(f"refresh_llm must be true or false, not {params['refresh_llm']!r}")
    profile = params.get("profile")
    if profile is not None and not (isinstance(profile, str) and profile.strip()):
        raise ValueError(f"profile must be a profile name, not {profile!r}")
    return ReasonParams(**params)


def _item_outcome(ctx: HandlerContext, name: str, status: str, reason: str | None = None) -> HandlerResult:
    """The item's new status (with the reason for `failed`/`deferred`), in one commit after the file effects.
    The job succeeds: an LLM or facts problem is an item state (decision 3)."""
    with session_scope(ctx.engine) as session:
        item = set_item_status(session, name, status, now=ctx.clock(), reason=reason)
    if item is None:
        return Fail(f"no item {name!r}: it was removed while the job ran")
    return Done({"item": status})


def _next_params(name: str, params: ReasonParams) -> dict[str, Any]:
    """The params of the item's next job: its name, plus `profile` and `refresh_llm` when set."""
    job_params: dict[str, Any] = {"calculated_name": name}
    if params.profile is not None:
        job_params["profile"] = params.profile
    if params.refresh_llm:
        job_params["refresh_llm"] = True
    return job_params


def _wait_for_youtube(ctx: HandlerContext, name: str, params: ReasonParams) -> HandlerResult:
    """No saved facts: the item waits for YouTube and its fetch job is queued again, in one commit. The
    dedupe key makes this a no-op while a fetch of this item is already queued or running."""
    job_params = _next_params(name, params)
    now = ctx.clock()
    with session_scope(ctx.engine) as session:
        item = set_item_status(session, name, "waiting_youtube", now=now)
        if item is None:
            return Fail(f"no item {name!r}: it was removed while the job ran")
        enqueue(session, type="youtube.fetch", now=now, params=job_params, dedupe_key=f"fetch:{name}")
    return Done({"item": "waiting_youtube"})


def _failed_reason(failed: Path) -> str:
    """The reason in the `.error.txt` next to a document in `failed/`, for a rerun after a crash."""
    try:
        for line in failed.with_suffix(".error.txt").read_text(encoding="utf-8").splitlines():
            if line.startswith("reason: "):
                return line.removeprefix("reason: ")
    except OSError:
        pass
    return f"moved to failed/{failed.name} by an earlier run"


def handle_llm_reason(ctx: HandlerContext, job: Job) -> HandlerResult:
    """Make the page of one staged document (param `calculated_name`) and record the item's outcome."""
    try:
        params = parse_reason_params(dict(job.params or {}))
    except ValueError as e:
        return Fail(str(e))
    name = params.calculated_name
    with session_scope(ctx.engine) as session:  # short: no transaction stays open during the model call
        item = get_item(session, name)
        status = item.status if item is not None else None
    if status is None:
        return Fail(f"no item {name!r}")
    if status in TERMINAL_STATUSES:  # this job ran before and its outcome is committed: nothing to redo
        log.info("%s: already %s, nothing to do", name, status)
        return Done({"item": status})
    try:
        return _reason(ctx, name, params)
    except OSError as e:  # a file error must not leave the item active with no job to move it
        reason = f"file error: {e}"
        log.error("%s: %s; the item is failed (a requeue runs it again)", name, reason)
        with session_scope(ctx.engine) as session:
            set_item_status(session, name, "failed", now=ctx.clock(), reason=reason)
        return Fail(reason)


def _reason(ctx: HandlerContext, name: str, params: ReasonParams) -> HandlerResult:
    """The work of `handle_llm_reason` once the item is known to be active. File errors propagate."""
    ideas, now = ctx.ideas, ctx.clock()
    out = ideas / "output" / name
    if not out.is_file():
        failed = ideas / "failed" / name
        if failed.is_file():  # a crash after the move to failed/, before the status was committed
            return _item_outcome(ctx, name, "failed", _failed_reason(failed))
        log.error("%s: no working copy in output/; marked failed", name)
        return _item_outcome(ctx, name, "failed", f"no working copy in output/{name}")
    try:
        note = load_staged_note(ideas, out, now)
    except (FrontmatterError, UnicodeDecodeError, ValueError) as e:
        reason = f"cannot read output/{name}: {e}"
        log.error("%s: %s", name, reason)
        move_to_failed(ideas, out, reason, now=now)
        return _item_outcome(ctx, name, "failed", reason)
    note.name = Path(name).name
    if note.doc.fm.get("stage") not in (STAGE_ANALYZED, STAGE_DEFERRED):
        # the final page is already in output/: a crash came after `finish`, before the status commit
        log.info("%s: the page was already made, marking it published", note_label(note))
        return _item_outcome(ctx, name, "published")

    opts = ProcessOptions(
        profile=params.profile,
        refresh_llm=params.refresh_llm,
        allow_fetch=False,  # facts come from youtube.fetch; this job never calls YouTube
        facts_dir=ideas / FACTS_DIR,
        llm_dir=ideas / LLM_DIR,
    )
    who = note_label(note)
    try:
        processed = process_note(note, ctx.services, opts)
    except Exception as e:  # an LLM or facts problem is the item's outcome, as in Stage A
        outcome = classify(e)
        log_outcome(who, outcome, outcome.message, e)
        if outcome.kind in ("would_fetch", "waiting"):  # no saved facts: back to youtube.fetch
            return _wait_for_youtube(ctx, name, params)
        state = RunState(blocked=set(), budget_blocked={}, attempted=1, seen_ids=set())
        report = ItemReport(note.doc_id, note.doctype.name, "skipped")
        apply_outcome(ideas, note, outcome, state, report, dry_run=False, now=now)
        result = _item_outcome(ctx, name, outcome.kind, report.message)
        if outcome.unexpected and isinstance(result, Done):  # a bug, not an item state: the job fails too
            return Fail(outcome.message)
        return result

    if processed.problems:
        reason = f"page is invalid: {'; '.join(processed.problems)}"
        log.error("%s: failed, %s", who, reason)
        reject_invalid_page(ideas, ideas / LLM_DIR, note, processed, ctx.services, reason, now)
        return _item_outcome(ctx, name, "failed", reason)
    write_page(ctx.docs, note.doctype, note.doc_id, processed.filename, processed.page)
    finish(ideas, note, processed)
    log.info("%s: published %s", who, processed.filename)
    return _item_outcome(ctx, name, "published")


def _wait_for_llm(ctx: HandlerContext, name: str, params: ReasonParams) -> HandlerResult:
    """The facts are saved: the item waits for the LLM and its `llm.reason` job is queued, in one commit. The
    dedupe key makes this a no-op while a reason job of this item is already queued or running."""
    now = ctx.clock()
    with session_scope(ctx.engine) as session:
        item = set_item_status(session, name, "waiting_llm", now=now)
        if item is None:
            return Fail(f"no item {name!r}: it was removed while the job ran")
        enqueue(
            session,
            type="llm.reason",
            now=now,
            params=_next_params(name, params),
            dedupe_key=f"reason:{name}",
        )
    return Done({"item": "waiting_llm"})


def handle_youtube_fetch(ctx: HandlerContext, job: Job) -> HandlerResult:
    """Get the facts of one staged clip (param `calculated_name`), then queue its `llm.reason`."""
    try:
        params = parse_reason_params(dict(job.params or {}), "youtube.fetch")
    except ValueError as e:
        return Fail(str(e))
    name = params.calculated_name
    with session_scope(ctx.engine) as session:  # short: no transaction stays open during the YouTube call
        item = get_item(session, name)
        status = item.status if item is not None else None
    if status is None:
        return Fail(f"no item {name!r}")
    if status != "waiting_youtube":  # this job ran before and its outcome is committed: nothing to redo
        log.info("%s: already %s, nothing to fetch", name, status)
        return Done({"item": status})
    try:
        return _fetch(ctx, name, params)
    except OSError as e:  # a file error must not leave the item active with no job to move it
        reason = f"file error: {e}"
        log.error("%s: %s; the item is failed (a requeue runs it again)", name, reason)
        with session_scope(ctx.engine) as session:
            set_item_status(session, name, "failed", now=ctx.clock(), reason=reason)
        return Fail(reason)


def _fetch(ctx: HandlerContext, name: str, params: ReasonParams) -> HandlerResult:
    """The work of `handle_youtube_fetch` once the item is known to wait for YouTube. File errors
    propagate."""
    ideas, now = ctx.ideas, ctx.clock()
    out = ideas / "output" / name
    if not out.is_file():
        failed = ideas / "failed" / name
        if failed.is_file():  # a crash after the move to failed/, before the status was committed
            return _item_outcome(ctx, name, "failed", _failed_reason(failed))
        log.error("%s: no working copy in output/; marked failed", name)
        return _item_outcome(ctx, name, "failed", f"no working copy in output/{name}")
    try:
        note = load_staged_note(ideas, out, now)
    except (FrontmatterError, UnicodeDecodeError, ValueError) as e:
        reason = f"cannot read output/{name}: {e}"
        log.error("%s: %s", name, reason)
        move_to_failed(ideas, out, reason, now=now)
        return _item_outcome(ctx, name, "failed", reason)
    note.name = Path(name).name

    # saved facts first (no call), then the gate, then YouTube; never a sleep: a closed gate defers the job
    opts = ProcessOptions(profile=params.profile, facts_dir=ideas / FACTS_DIR, wait_youtube=False)
    who = note_label(note)
    try:
        get_facts(note, ctx.services, opts)
    except FactsDeferred as e:  # the gap or the breaker: the job waits for the gate, the item keeps waiting
        until = (
            datetime.fromtimestamp(e.until, UTC)
            if e.until is not None
            else ctx.clock() + timedelta(seconds=DEFER_FALLBACK_S)
        )
        log.info("%s: %s; the fetch waits until %s", who, e, until.isoformat(timespec="seconds"))
        return Defer(run_after=until, reason=str(e))
    except (
        Exception
    ) as e:  # no facts to be had (deferred) or a bug (failed): the item's outcome, as in Stage A
        outcome = classify(e)
        log_outcome(who, outcome, outcome.message, e)
        state = RunState(blocked=set(), budget_blocked={}, attempted=1, seen_ids=set())
        report = ItemReport(note.doc_id, note.doctype.name, "skipped")
        apply_outcome(ideas, note, outcome, state, report, dry_run=False, now=now)
        result = _item_outcome(ctx, name, outcome.kind, report.message)
        if outcome.unexpected and isinstance(result, Done):  # a bug, not an item state: the job fails too
            return Fail(outcome.message)
        return result
    log.info("%s: facts saved, on to the LLM", who)
    return _wait_for_llm(ctx, name, params)


IDEAS_MANAGED = ("inbox", "archive", "output", "failed", "duplicates", FACTS_DIR, LLM_DIR)
DOCS_MANAGED = (*sorted({t.out_dir for t in DOC_TYPES.values()}), f"idea-bucket/{ARTIFACTS_DIR}")
IDEAS_MESSAGE = "idea-catcher: process the inbox (pipeline.publish)"
DOCS_MESSAGE = "idea-catcher: publish pages (pipeline.publish)"


def parse_publish_params(params: dict[str, Any]) -> tuple[bool, bool]:
    """`(pull, push)` from the job's params. Raises ValueError with a message that names the bad parameter."""
    unknown = sorted(set(params) - {"pull", "push"})
    if unknown:
        raise ValueError(f"unknown parameter(s) for pipeline.publish: {', '.join(unknown)}")
    for key in ("pull", "push"):
        if not isinstance(params.get(key, True), bool):
            raise ValueError(f"{key} must be true or false, not {params[key]!r}")
    return params.get("pull", True), params.get("push", True)


def handle_pipeline_publish(ctx: HandlerContext, job: Job) -> HandlerResult:
    """Per repo (idea-bucket first): commit the managed folders, pull (rebase), push right after.

    A repo is pushed when it is ahead of its upstream, so a rerun pushes a commit a failed pull or push left
    behind. A git error fails the job with git's message and stops it there; a commit already made stays
    local, and a failed rebase is aborted first. `push=true` on a repo without a remote fails before any
    git change."""
    try:
        do_pull, do_push = parse_publish_params(dict(job.params or {}))
    except ValueError as e:
        return Fail(str(e))
    repos = (
        ("ideas", ctx.ideas, IDEAS_MANAGED, IDEAS_MESSAGE),
        ("docs", ctx.docs, DOCS_MANAGED, DOCS_MESSAGE),
    )
    author = (ctx.settings.git_author_name, ctx.settings.git_author_email)
    committed = {"docs": False, "ideas": False}
    pushed = False
    with GIT_LOCK:
        try:
            if do_push:
                no_remote = [str(repo) for _, repo, _, _ in repos if not has_remote(repo)]
                if no_remote:
                    return Fail(
                        f"no git remote to push to in {', '.join(no_remote)}: "
                        "add a remote, or run pipeline.publish with push=false"
                    )
            for key, repo, managed, message in repos:
                committed[key] = commit_managed(repo, managed, message, author=author)
                if do_pull:  # after the commit: the autostash only ever holds changes outside the managed set
                    pull(repo, author=author)
                if do_push and ahead_of_upstream(repo):
                    push(repo, author=author)
                    pushed = True
        except GitError as e:
            log.error("publish failed (committed so far: %s): %s", committed, e)
            return Fail(str(e))
    log.info("publish: committed=%s pushed=%s", committed, pushed)
    return Done({"committed": committed, "pushed": pushed})
