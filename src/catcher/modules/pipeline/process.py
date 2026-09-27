from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from catcher.core.config import Settings
from catcher.modules.llm.backends import make_backend
from catcher.modules.llm.profiles import Profile, ProfilesConfig, load_profiles, resolve_profile
from catcher.modules.llm.schemas import Summary
from catcher.modules.llm.service import BackendFactory, LlmRequest, LlmResult, UsageLimitReached, reason
from catcher.modules.pipeline.inputs import capture_tags, prompt_input
from catcher.modules.pipeline.render import PageContext, page_name, render_page
from catcher.modules.pipeline.staging import StagedNote
from catcher.modules.pipeline.tags import TagList, load_tags, normalize_tags
from catcher.modules.pipeline.validate import validate_page


@dataclass
class Services:
    settings: Settings
    profiles: ProfilesConfig
    backends: BackendFactory
    tags: TagList


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


def default_services(settings: Settings) -> Services:
    return Services(
        settings=settings,
        profiles=load_profiles(settings.profiles_file),
        backends=lambda p: make_backend(p, settings),
        tags=load_tags(),
    )


def check_not_blocked(profile: Profile, opts: ProcessOptions) -> None:
    if profile.backend in opts.blocked_backends:
        raise UsageLimitReached("usage limit was reached earlier in this run", backend=profile.backend)


def process_note(note: StagedNote, svc: Services, opts: ProcessOptions) -> ProcessedPage:
    profile_name, profile = resolve_profile(
        svc.profiles, requested=opts.profile, class_default=note.doctype.llm_profile
    )
    check_not_blocked(profile, opts)
    request = LlmRequest(
        task=note.doctype.task,
        input=prompt_input(note, svc.tags),
        schema_name=note.doctype.schema_name,
        profile=profile_name,
    )
    result = reason(request, profiles=svc.profiles, backends=svc.backends)
    summary = cast(Summary, result.output)
    tag_result = normalize_tags([*summary.tags, *capture_tags(note)], svc.tags)
    ctx = PageContext(note=note, summary=summary, tags=tag_result.tags, llm=result)
    page = render_page(ctx)
    return ProcessedPage(
        note, page_name(ctx), page, validate_page(page, svc.tags), result, tag_result.dropped
    )
