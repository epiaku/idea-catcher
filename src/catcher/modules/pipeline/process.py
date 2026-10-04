import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from catcher.core.config import Settings
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.profiles import Profile, ProfilesConfig, load_profiles, resolve_profile
from catcher.modules.llm.schemas import Summary, YoutubeSummary
from catcher.modules.llm.service import (
    BackendFactory,
    InputRejected,
    LlmRequest,
    LlmResult,
    LlmTrace,
    Recorder,
    UsageLimitReached,
    reason,
)
from catcher.modules.llm.trace import TraceStore
from catcher.modules.pipeline.context import load_context
from catcher.modules.pipeline.doctypes import gemini_video_id
from catcher.modules.pipeline.glossary import Glossary, load_glossary
from catcher.modules.pipeline.inbox import Note, move_to_failed, note_label
from catcher.modules.pipeline.inputs import capture_tags, prompt_input
from catcher.modules.pipeline.render import PageContext, page_name, render_page
from catcher.modules.pipeline.tags import TagList, load_tags, normalize_tags
from catcher.modules.pipeline.validate import validate_page
from catcher.modules.youtube.access import YoutubeAccess, build_access
from catcher.modules.youtube.cache import FactsCache
from catcher.modules.youtube.checks import SummaryWarning, check_summary, verified_links
from catcher.modules.youtube.facts import (
    FactsFetcher,
    FactsUnavailable,
    FetchSkipped,
    YoutubeFacts,
    fetch_facts,
)
from catcher.modules.youtube.urls import video_id

log = logging.getLogger("catcher.process")


@dataclass
class Services:
    settings: Settings
    profiles: ProfilesConfig
    backends: BackendFactory
    tags: TagList
    facts: FactsFetcher = fetch_facts
    glossary: Glossary = field(default_factory=Glossary)
    context: str = ""  # who Epiaku is, for the Channel Application part of YouTube summaries
    youtube: YoutubeAccess | None = (
        None  # the saved facts, the gap and the breaker. None: fetch directly (tests)
    )


@dataclass
class ProcessOptions:
    profile: str | None = None
    dry_run: bool = False
    blocked_backends: frozenset[str] = frozenset()
    facts_dir: Path | None = (
        None  # where the saved YouTube facts live (`facts/` in idea-bucket); None: no saving
    )
    refresh_facts: bool = False  # fetch again even when facts are saved
    wait_youtube: bool = False  # sleep through a short gap instead of deferring
    llm_dir: Path | None = None  # where the LLM traces go (`llm/` in idea-bucket); None: no trace
    refresh_llm: bool = False  # call the model even when a good reply is saved in `llm_dir`
    allow_fetch: bool = True  # False: saved facts only, YouTube is never asked (the `llm.reason` job)


@dataclass
class ProcessedPage:
    note: Note
    filename: str
    page: str
    problems: list[str]
    llm: LlmResult
    dropped_tags: list[str]
    warnings: list[SummaryWarning] = field(default_factory=list)  # YouTube only: free, non-LLM checks
    facts: YoutubeFacts | None = None  # YouTube only: written next to the final page


def default_services(settings: Settings) -> Services:
    access = build_access(settings)
    return Services(
        settings=settings,
        profiles=load_profiles(settings.profiles_file),
        backends=lambda p: make_backend(p, settings),
        tags=load_tags(),
        facts=access.fetch,
        glossary=load_glossary(),
        context=load_context(),
        youtube=access,
    )


def check_not_blocked(profile: Profile, opts: ProcessOptions) -> None:
    if profile.backend in opts.blocked_backends:
        raise UsageLimitReached("usage limit was reached earlier in this run", backend=profile.backend)


def facts_for(note: Note, vid: str, svc: Services, opts: ProcessOptions) -> YoutubeFacts:
    log.debug("%s: getting the YouTube facts for %s", note_label(note), vid)
    if svc.youtube is None:
        if not opts.allow_fetch:  # no access layer (tests): only the saved facts, never the fetcher
            saved = FactsCache(opts.facts_dir).get(vid) if opts.facts_dir else None
            if saved is None:
                raise FetchSkipped(f"no saved facts for {vid}: fetching from YouTube is not allowed here")
            return saved
        facts = svc.facts(vid)
    else:  # saved facts first, then the gap and the breaker, then YouTube
        facts = svc.youtube.get(
            vid,
            facts_dir=opts.facts_dir,
            refresh=opts.refresh_facts,
            write_cache=not opts.dry_run,
            wait=opts.wait_youtube,
            fetch_allowed=opts.allow_fetch and not opts.dry_run,  # a dry run never costs YouTube a request
        )
    log.info(
        "%s: facts fetched (views=%s transcript=%s)",
        note_label(note),
        facts.views,
        "yes" if facts.transcript else "no",
    )
    return facts


def youtube_embed(vid: str, title: str) -> str:
    return "{{< youtube-lite " + vid + " `" + title.replace("`", "'") + "` >}}"


def log_llm(note: Note, step: str, result: LlmResult) -> None:
    log.debug(
        "%s: %s done (backend=%s model=%s attempts=%d tokens_in=%s tokens_out=%s)",
        note_label(note),
        step,
        result.backend,
        result.model,
        result.attempts,
        result.usage.tokens_in,
        result.usage.tokens_out,
    )


def trace_recorder(note: Note, llm_dir: Path) -> Recorder:
    """Write the trace of this document's LLM call to `llm/<subfolder>/<calculated name>.json`. A trace that
    cannot be written is logged and skipped: it must never fail the document."""

    def record(trace: LlmTrace) -> None:
        try:
            TraceStore(llm_dir).put(note.target_rel, trace, now=datetime.now(UTC))
        except Exception as e:
            log.warning("%s: could not write the LLM trace: %s", note_label(note), e)

    return record


def ask_llm(
    note: Note,
    svc: Services,
    profile_name: str,
    facts: YoutubeFacts | None = None,
    *,
    llm_dir: Path | None = None,
    dry_run: bool = False,
    refresh_llm: bool = False,
) -> LlmResult:
    if facts is None and not note.doc.body.strip():
        raise InputRejected(f"{note.doc_id}: the document is empty")
    prompt = prompt_input(note, svc.tags, facts, svc.glossary, svc.context)
    size = len(json.dumps(prompt, ensure_ascii=False, default=str))
    if size > svc.settings.llm_max_input_chars:
        raise InputRejected(
            f"{note.doc_id}: {size:,} characters is over the {svc.settings.llm_max_input_chars:,} "
            "limit (LLM_MAX_INPUT_CHARS): not sent to the LLM"
        )
    request = LlmRequest(
        task=note.doctype.task,
        input=prompt,
        schema_name=note.doctype.schema_name,
        profile=profile_name,
    )
    log.debug("%s: asking the LLM (profile=%s)", note_label(note), profile_name)
    recorder = None
    if llm_dir is not None and svc.settings.llm_trace and not dry_run:
        recorder = trace_recorder(note, llm_dir)
    replayer = None  # a dry run reads saved replies too: it costs nothing and writes nothing
    if llm_dir is not None and svc.settings.llm_cache and not refresh_llm:
        replayer = TraceStore(llm_dir).replayer()
    result = reason(
        request,
        profiles=svc.profiles,
        backends=svc.backends,
        recorder=recorder,
        replayer=replayer,
        keep_prompt=svc.settings.llm_trace_prompt,
    )
    if result.from_saved:
        log.debug("%s: using the saved LLM reply (no call)", note_label(note))
    log_llm(note, "reason", result)
    return result


def get_facts(note: Note, svc: Services, opts: ProcessOptions) -> YoutubeFacts | None:
    """Everything before the LLM. `youtube`: the real facts (no transcript or an unavailable video
    raises FactsUnavailable, so the document is deferred instead of paying for an LLM call).
    `youtube-gemini`: no YouTube call at all, only the check that the chat holds a video id. Text
    classes: None."""
    if note.doctype.name == "youtube":
        vid = video_id(str(note.doc.fm.get("source") or ""))
        if not vid:
            raise FactsUnavailable(f"{note.doc_id}: no YouTube video id found")
        facts = facts_for(note, vid, svc, opts)
        if facts.unavailable_reason:
            raise FactsUnavailable(
                f"{note.doc_id}: YouTube says {vid} is unavailable: {facts.unavailable_reason}"
            )
        if not facts.transcript:
            raise FactsUnavailable(f"{note.doc_id}: no transcript available for {vid}")
        return facts
    if note.doctype.name == "youtube-gemini" and not gemini_video_id(note.doc.body):
        raise FactsUnavailable(f"{note.doc_id}: no YouTube video id found in the Gemini chat")
    return None


def build_page(note: Note, svc: Services, result: LlmResult, facts: YoutubeFacts | None) -> ProcessedPage:
    """Tags, the free checks, render and validate. Needs no backend: the LLM result is already made."""
    if note.doctype.name == "youtube":
        return _build_youtube(note, svc, result, cast(YoutubeFacts, facts))
    if note.doctype.name == "youtube-gemini":
        return _build_youtube_gemini(note, svc, result)
    return _build_text(note, svc, result)


def _build_text(note: Note, svc: Services, result: LlmResult) -> ProcessedPage:
    summary = cast(Summary, result.output)
    tag_result = normalize_tags([*summary.tags, *capture_tags(note)], svc.tags)
    ctx = PageContext(note=note, summary=summary, tags=tag_result.tags, llm=result)
    language = getattr(summary, "language", None)  # only a note says which language it was written in
    page = render_page(ctx, extra_fm={"language": language} if language else None)
    return ProcessedPage(
        note, page_name(ctx), page, validate_page(page, svc.tags), result, tag_result.dropped
    )


def _build_youtube(note: Note, svc: Services, result: LlmResult, facts: YoutubeFacts) -> ProcessedPage:
    """The `youtube` class: the summary plus the free, non-LLM checks against the fetched facts."""
    vid = video_id(str(note.doc.fm.get("source") or ""))
    summary = cast(YoutubeSummary, result.output)
    links = verified_links(summary.links, facts.description)
    if len(links) != len(summary.links):
        log.warning(
            "%s: dropped %d link(s) that are not in the video description",
            note_label(note),
            len(summary.links) - len(links),
        )
    summary = summary.model_copy(
        update={"links": links, "chapters": [], "metrics": None}
    )  # all from the facts
    warnings = check_summary(summary, facts)
    if warnings:
        log.warning("%s: %d warning(s) on the summary", note_label(note), len(warnings))
    tag_result = normalize_tags([*summary.tags, *capture_tags(note)], svc.tags)
    ctx = PageContext(note=note, summary=summary, tags=tag_result.tags, llm=result)
    extra_fm: dict[str, object] = {"video_id": vid}
    if warnings:
        extra_fm["warnings"] = [w.frontmatter() for w in warnings]
    page = render_page(
        ctx,
        extra_fm=extra_fm,
        facts=facts,
        warnings=warnings,
        embed=youtube_embed(str(vid), facts.title or summary.title),
    )
    problems = validate_page(page, svc.tags)
    return ProcessedPage(note, page_name(ctx), page, problems, result, tag_result.dropped, warnings, facts)


def _build_youtube_gemini(note: Note, svc: Services, result: LlmResult) -> ProcessedPage:
    """The `youtube-gemini` class: Gemini's own answer reformatted, no facts, no free checks (there is
    nothing to check the summary against). Gemini's own metrics are shown as it wrote them."""
    vid = str(gemini_video_id(note.doc.body))
    summary = cast(YoutubeSummary, result.output).model_copy(update={"links": []})  # no description to check
    tag_result = normalize_tags([*summary.tags, *capture_tags(note)], svc.tags)
    ctx = PageContext(note=note, summary=summary, tags=tag_result.tags, llm=result)
    page = render_page(
        ctx,
        extra_fm={"video_id": vid},
        facts=None,
        warnings=[],
        embed=youtube_embed(vid, summary.title),
    )
    problems = validate_page(page, svc.tags)
    return ProcessedPage(note, page_name(ctx), page, problems, result, tag_result.dropped)


def process_note(note: Note, svc: Services, opts: ProcessOptions) -> ProcessedPage:
    profile_name, profile = resolve_profile(
        svc.profiles, requested=opts.profile, class_default=note.doctype.llm_profile
    )
    check_not_blocked(profile, opts)
    facts = get_facts(note, svc, opts)
    result = ask_llm(
        note,
        svc,
        profile_name,
        facts,
        llm_dir=opts.llm_dir,
        dry_run=opts.dry_run,
        refresh_llm=opts.refresh_llm,
    )
    return build_page(note, svc, result, facts)


def mark_unusable(
    llm_dir: Path, note: Note, processed: ProcessedPage, svc: Services, reason: str
) -> list[Path]:
    """A reply that made an invalid page is not a good one: mark the trace it came from, so a plain requeue
    asks the model again. That is the file a saved reply was read from (maybe another document's), else this
    document's own trace, which the store only marks when it holds this very reply. With LLM_TRACE=false
    nothing in `llm/` is written. Returns the marked file, to commit."""
    llm = processed.llm
    if not svc.settings.llm_trace or llm.content_key is None:
        return []
    path = llm.saved_from or TraceStore(llm_dir).path_for(note.target_rel)
    output = llm.output.model_dump(mode="json")
    marked = TraceStore(llm_dir).mark_unusable(
        path, reason=reason, output=output, content_key=llm.content_key, backend=llm.backend
    )
    return [path] if marked else []


def reject_invalid_page(
    ideas: Path,
    llm_dir: Path,
    note: Note,
    processed: ProcessedPage,
    svc: Services,
    reason: str,
    now: datetime | None = None,
) -> list[Path]:
    """The page did not validate: mark the trace of the reply that made it (`mark_unusable`) and move the
    working copy in `output/` to `failed/` with `reason`. Shared by `catcher run pipeline` and the
    `llm.reason` job. Returns the touched idea-bucket paths."""
    touched = mark_unusable(llm_dir, note, processed, svc, reason)
    touched += move_to_failed(
        ideas, note.output_path(ideas), reason, doc_id=note.doc_id, doc_class=note.doctype.name, now=now
    )
    return touched
