from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from catcher.core.config import Settings
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.profiles import Profile, ProfilesConfig, load_profiles, resolve_profile
from catcher.modules.llm.schemas import Summary, YoutubeSummary
from catcher.modules.llm.service import BackendFactory, LlmRequest, LlmResult, UsageLimitReached, reason
from catcher.modules.pipeline.doctypes import gemini_video_id
from catcher.modules.pipeline.inputs import capture_tags, prompt_input
from catcher.modules.pipeline.publish import find_pages_by_id
from catcher.modules.pipeline.render import PageContext, page_name, render_page
from catcher.modules.pipeline.staging import StagedNote, facts_sidecar
from catcher.modules.pipeline.tags import TagList, load_tags, normalize_tags
from catcher.modules.pipeline.validate import validate_page
from catcher.modules.youtube.facts import FactsFetcher, FactsUnavailable, YoutubeFacts, fetch_facts
from catcher.modules.youtube.review import ReviewOutcome, not_reviewed, review_summary
from catcher.modules.youtube.urls import video_id

YOUTUBE_CLASSES = ("youtube", "youtube-gemini")


@dataclass
class Services:
    settings: Settings
    profiles: ProfilesConfig
    backends: BackendFactory
    tags: TagList
    facts: FactsFetcher = fetch_facts


@dataclass
class ProcessOptions:
    profile: str | None = None
    review: bool = True
    review_profile: str | None = None
    dry_run: bool = False
    docs_repo: Path | None = None
    blocked_backends: frozenset[str] = frozenset()


@dataclass
class ProcessedPage:
    note: StagedNote
    filename: str
    page: str
    problems: list[str]
    llm: LlmResult
    dropped_tags: list[str]
    written: list[Path] = field(default_factory=list)
    review: ReviewOutcome | None = None


def default_services(settings: Settings) -> Services:
    languages = settings.transcript_language_list
    return Services(
        settings=settings,
        profiles=load_profiles(settings.profiles_file),
        backends=lambda p: make_backend(p, settings),
        tags=load_tags(),
        facts=lambda vid: fetch_facts(vid, languages=languages),
    )


def check_not_blocked(profile: Profile, opts: ProcessOptions) -> None:
    if profile.backend in opts.blocked_backends:
        raise UsageLimitReached("usage limit was reached earlier in this run", backend=profile.backend)


def review_profile(svc: Services, opts: ProcessOptions) -> tuple[str, Profile]:
    return resolve_profile(
        svc.profiles, requested=opts.review_profile, class_default=svc.profiles.review_profile
    )


def youtube_video_id(note: StagedNote) -> str:
    if note.doctype.name == "youtube":
        vid = video_id(str(note.doc.fm.get("source") or ""))
    else:
        vid = gemini_video_id(note.doc.body)
    if not vid:
        raise FactsUnavailable(f"{note.doc_id}: no YouTube video id found")
    return vid


def facts_for(
    note: StagedNote, vid: str, svc: Services, opts: ProcessOptions
) -> tuple[YoutubeFacts, list[Path]]:
    sidecar = facts_sidecar(note.path)
    if sidecar.exists():
        return YoutubeFacts.model_validate_json(sidecar.read_text(encoding="utf-8")), []
    facts = svc.facts(vid)
    if opts.dry_run:
        return facts, []
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(facts.model_dump_json(indent=2), encoding="utf-8")
    return facts, [sidecar]


def sibling_link(note: StagedNote, vid: str, opts: ProcessOptions) -> dict[str, str | None]:
    none: dict[str, str | None] = {"sibling": None, "sibling_label": None}
    if opts.docs_repo is None:
        return none
    other = f"{vid}-gemini" if note.doctype.name == "youtube" else vid
    pages = find_pages_by_id(opts.docs_repo / note.doctype.out_dir, other)
    if not pages:
        return none
    label = "from a Gemini web chat" if note.doctype.name == "youtube" else "directly from the YouTube clip"
    return {"sibling": f"../{pages[0].stem.lower()}/", "sibling_label": label}


def youtube_embed(vid: str, title: str) -> str:
    return "{{< youtube-lite " + vid + " `" + title.replace("`", "'") + "` >}}"


def _reason(
    note: StagedNote, svc: Services, profile_name: str, facts: YoutubeFacts | None = None
) -> LlmResult:
    request = LlmRequest(
        task=note.doctype.task,
        input=prompt_input(note, svc.tags, facts),
        schema_name=note.doctype.schema_name,
        profile=profile_name,
    )
    return reason(request, profiles=svc.profiles, backends=svc.backends)


def _process_text(note: StagedNote, svc: Services, profile_name: str) -> ProcessedPage:
    result = _reason(note, svc, profile_name)
    summary = cast(Summary, result.output)
    tag_result = normalize_tags([*summary.tags, *capture_tags(note)], svc.tags)
    ctx = PageContext(note=note, summary=summary, tags=tag_result.tags, llm=result)
    page = render_page(ctx)
    return ProcessedPage(
        note, page_name(ctx), page, validate_page(page, svc.tags), result, tag_result.dropped
    )


def _process_youtube(
    note: StagedNote, svc: Services, opts: ProcessOptions, profile_name: str
) -> ProcessedPage:
    review_name: str | None = None
    if opts.review:
        review_name, review_prof = review_profile(svc, opts)
        check_not_blocked(review_prof, opts)
    vid = youtube_video_id(note)
    facts, written = facts_for(note, vid, svc, opts)

    def run_review(text: str) -> ReviewOutcome:
        assert review_name is not None
        return review_summary(
            text, facts, profile=review_name, profiles=svc.profiles, backends=svc.backends, tags=svc.tags
        )

    if note.doctype.name == "youtube-gemini" and review_name:
        outcome = run_review(note.doc.body)
        result = cast(LlmResult, outcome.llm)
    else:
        result = _reason(note, svc, profile_name, facts)
        first = cast(YoutubeSummary, result.output)
        outcome = run_review(first.model_dump_json(indent=2)) if review_name else not_reviewed(first)

    summary = outcome.summary
    tag_result = normalize_tags([*summary.tags, *capture_tags(note)], svc.tags)
    ctx = PageContext(note=note, summary=summary, tags=tag_result.tags, llm=result)
    page = render_page(
        ctx,
        extra_fm={"video_id": vid, "review": outcome.frontmatter()},
        facts=facts,
        review=outcome,
        embed=youtube_embed(vid, facts.title or summary.title),
        **sibling_link(note, vid, opts),
    )
    problems = validate_page(page, svc.tags)
    return ProcessedPage(note, page_name(ctx), page, problems, result, tag_result.dropped, written, outcome)


def process_note(note: StagedNote, svc: Services, opts: ProcessOptions) -> ProcessedPage:
    profile_name, profile = resolve_profile(
        svc.profiles, requested=opts.profile, class_default=note.doctype.llm_profile
    )
    check_not_blocked(profile, opts)
    if note.doctype.name in YOUTUBE_CLASSES:
        return _process_youtube(note, svc, opts, profile_name)
    return _process_text(note, svc, profile_name)
