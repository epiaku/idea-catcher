import logging
from dataclasses import dataclass, field
from typing import cast

from catcher.core.config import Settings
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.profiles import Profile, ProfilesConfig, load_profiles, resolve_profile
from catcher.modules.llm.schemas import Summary, YoutubeSummary
from catcher.modules.llm.service import BackendFactory, LlmRequest, LlmResult, UsageLimitReached, reason
from catcher.modules.pipeline.context import load_context
from catcher.modules.pipeline.doctypes import gemini_video_id
from catcher.modules.pipeline.glossary import Glossary, load_glossary
from catcher.modules.pipeline.inbox import Note, note_label
from catcher.modules.pipeline.inputs import capture_tags, prompt_input
from catcher.modules.pipeline.render import PageContext, page_name, render_page
from catcher.modules.pipeline.tags import TagList, load_tags, normalize_tags
from catcher.modules.pipeline.validate import validate_page
from catcher.modules.youtube.checks import SummaryWarning, check_summary, verified_links
from catcher.modules.youtube.facts import FactsFetcher, FactsUnavailable, YoutubeFacts, fetch_facts
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


@dataclass
class ProcessOptions:
    profile: str | None = None
    dry_run: bool = False
    blocked_backends: frozenset[str] = frozenset()


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
    languages = settings.transcript_language_list
    return Services(
        settings=settings,
        profiles=load_profiles(settings.profiles_file),
        backends=lambda p: make_backend(p, settings),
        tags=load_tags(),
        facts=lambda vid: fetch_facts(vid, languages=languages),
        glossary=load_glossary(),
        context=load_context(),
    )


def check_not_blocked(profile: Profile, opts: ProcessOptions) -> None:
    if profile.backend in opts.blocked_backends:
        raise UsageLimitReached("usage limit was reached earlier in this run", backend=profile.backend)


def facts_for(note: Note, vid: str, svc: Services) -> YoutubeFacts:
    log.info("%s: fetching YouTube facts for %s", note_label(note), vid)
    facts = svc.facts(vid)
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
    log.info(
        "%s: %s done (backend=%s model=%s attempts=%d tokens_in=%s tokens_out=%s)",
        note_label(note),
        step,
        result.backend,
        result.model,
        result.attempts,
        result.usage.tokens_in,
        result.usage.tokens_out,
    )


def _reason(note: Note, svc: Services, profile_name: str, facts: YoutubeFacts | None = None) -> LlmResult:
    request = LlmRequest(
        task=note.doctype.task,
        input=prompt_input(note, svc.tags, facts, svc.glossary, svc.context),
        schema_name=note.doctype.schema_name,
        profile=profile_name,
    )
    log.info("%s: asking the LLM (profile=%s)", note_label(note), profile_name)
    result = reason(request, profiles=svc.profiles, backends=svc.backends)
    log_llm(note, "reason", result)
    return result


def _process_text(note: Note, svc: Services, profile_name: str) -> ProcessedPage:
    result = _reason(note, svc, profile_name)
    summary = cast(Summary, result.output)
    tag_result = normalize_tags([*summary.tags, *capture_tags(note)], svc.tags)
    ctx = PageContext(note=note, summary=summary, tags=tag_result.tags, llm=result)
    language = getattr(summary, "language", None)  # only a note says which language it was written in
    page = render_page(ctx, extra_fm={"language": language} if language else None)
    return ProcessedPage(
        note, page_name(ctx), page, validate_page(page, svc.tags), result, tag_result.dropped
    )


def _process_youtube(note: Note, svc: Services, opts: ProcessOptions, profile_name: str) -> ProcessedPage:
    """The `youtube` class: one LLM call summarizing the real transcript, plus the free, non-LLM checks
    against the fetched facts. Without a transcript there is nothing worth paying for an LLM call over
    (just a title and description), so that defers the document instead of asking the LLM."""
    vid = video_id(str(note.doc.fm.get("source") or ""))
    if not vid:
        raise FactsUnavailable(f"{note.doc_id}: no YouTube video id found")
    facts = facts_for(note, vid, svc)
    if not facts.transcript:
        raise FactsUnavailable(f"{note.doc_id}: no transcript available for {vid}")
    result = _reason(note, svc, profile_name, facts)
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
        embed=youtube_embed(vid, facts.title or summary.title),
    )
    problems = validate_page(page, svc.tags)
    return ProcessedPage(note, page_name(ctx), page, problems, result, tag_result.dropped, warnings, facts)


def _process_youtube_gemini(
    note: Note, svc: Services, opts: ProcessOptions, profile_name: str
) -> ProcessedPage:
    """The `youtube-gemini` class: reformats Gemini's own answer with no YouTube API call at all, not
    even for the video id (which is parsed straight out of the chat text). One LLM call, no facts, no
    free checks (there is nothing to check the summary against). Gemini's own metrics are shown as it
    wrote them."""
    vid = gemini_video_id(note.doc.body)
    if not vid:
        raise FactsUnavailable(f"{note.doc_id}: no YouTube video id found in the Gemini chat")
    result = _reason(note, svc, profile_name)
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
    if note.doctype.name == "youtube":
        return _process_youtube(note, svc, opts, profile_name)
    if note.doctype.name == "youtube-gemini":
        return _process_youtube_gemini(note, svc, opts, profile_name)
    return _process_text(note, svc, profile_name)
