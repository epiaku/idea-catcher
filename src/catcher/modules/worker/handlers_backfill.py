"""The backfill handlers.

`backfill.docs`: scan the docs repo for YouTube links, store the videos without a page in the backlog and,
when asked, release some of the backlog within the daily cap. The docs repo is only pulled here (never
committed apart from the worker's own pages, which are committed locally before the pull, as `ideas.pull`
does); a dry run pulls nothing and writes nothing.

`backfill.channel`: list one channel or playlist through the YouTube gate (one slot), store its unknown videos
in the backlog and, when asked, release some within the daily cap. A closed gate defers the job (never a
sleep); the claim leaves the job queued while the gate is closed."""

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from catcher.core.db import session_scope
from catcher.core.git import GIT_LOCK, GitError
from catcher.modules.backfill import store
from catcher.modules.backfill.channel import (
    DEFAULT_CHANNEL_VIDEOS,
    MAX_CHANNEL_VIDEOS,
    ChannelListingError,
    Extractor,
    build_extractor,
    is_channel_url,
    list_channel,
    listing_url,
)
from catcher.modules.backfill.importer import run_import
from catcher.modules.backfill.service import listing_fit, release_within, store_listing
from catcher.modules.queue.models import Job
from catcher.modules.worker.handlers import Defer, Done, Fail, HandlerContext, HandlerResult
from catcher.modules.worker.handlers_pipeline import (
    DEFER_FALLBACK_S,
    DOCS_MANAGED,
    DOCS_MESSAGE,
    commit_then_pull,
)
from catcher.modules.youtube.facts import FactsDeferred, FactsUnavailable

log = logging.getLogger("catcher.worker.backfill")

DOCS_PARAMS = frozenset({"dry_run", "pull", "release"})


def parse_release(params: dict[str, Any]) -> bool | int:
    """`release`: absent or null is true (the default limit); true, false or a whole number of 0 or more."""
    release = params.get("release")
    if release is None or isinstance(release, bool):
        return True if release is None else release
    if not isinstance(release, int) or release < 0:
        raise ValueError(f"release must be true, false or a whole number of 0 or more, not {release!r}")
    return release


def release_cap(value: bool | int, default_limit: int) -> int:
    """The 24 h cap to release within: true is the default limit, false is 0, a number is itself."""
    if isinstance(value, bool):
        return default_limit if value else 0
    return value


@dataclass(frozen=True)
class DocsParams:
    dry_run: bool = False
    pull: bool = True
    release: bool | int = True


def parse_docs_params(params: dict[str, Any]) -> DocsParams:
    """The `backfill.docs` params; raises ValueError naming what is wrong."""
    unknown = sorted(set(params) - DOCS_PARAMS)
    if unknown:
        raise ValueError(f"unknown parameter(s) for backfill.docs: {', '.join(unknown)}")
    for flag in ("dry_run", "pull"):
        if flag in params and not isinstance(params[flag], bool):
            raise ValueError(f"{flag} must be true or false, not {params[flag]!r}")
    release = parse_release(params)
    return DocsParams(dry_run=params.get("dry_run", False), pull=params.get("pull", True), release=release)


def handle_backfill_docs(ctx: HandlerContext, job: Job) -> HandlerResult:
    """Pull the docs repo (unless `pull` is false or `dry_run`), store the new links as pending and, with
    `release`, release up to that daily cap. A failed pull is `Fail(git's message)` and stores nothing."""
    try:
        params = parse_docs_params(dict(job.params or {}))
    except ValueError as e:
        return Fail(str(e))
    tag = f"backfill.docs {job.id}"
    if params.pull and not params.dry_run:
        author = (ctx.settings.git_author_name, ctx.settings.git_author_email)
        with GIT_LOCK:
            try:
                if commit_then_pull(ctx.docs, DOCS_MANAGED, DOCS_MESSAGE, author=author):
                    log.info("%s: pulled %s", tag, ctx.docs)
                else:
                    log.info("%s: %s has no git remote, nothing to pull", tag, ctx.docs)
            except GitError as e:
                log.error("%s: pull failed: %s", tag, e)
                return Fail(str(e))
    now = ctx.clock()
    allowance: int | None = None
    released: list[str] = []
    repaired: list[str] = []
    still_pending = 0
    with session_scope(ctx.engine) as session:
        imported = run_import(session, ctx.ideas, ctx.docs, now, dry_run=params.dry_run)
        log.info("%s: found %d, new %d, known %d", tag, imported.found, imported.new, imported.known)
        still_pending = imported.pending
        cap = release_cap(params.release, ctx.settings.backfill_daily_limit)
        if not params.dry_run:
            allowance = 0
            if cap > 0:
                allowance, outcome = release_within(session, ctx.ideas, cap, now, only=imported.found_ids)
                released, repaired = outcome.released, outcome.repaired
                still_pending = store.counts(session)["pending"]
                log.info(
                    "%s: allowance %d, released %d, repaired %d",
                    tag,
                    allowance,
                    len(released),
                    len(repaired),
                )
    result = asdict(imported)
    del result["new_ids"], result["found_ids"]
    result.update(
        dry_run=params.dry_run,
        released=released,
        repaired=repaired,
        still_pending=still_pending,
        allowance=allowance,
    )
    return Done(result)


CHANNEL_PARAMS = frozenset({"url", "max_videos", "release"})

# The extractor of a listing (yt-dlp). Tests replace it with a fake; production never assigns it.
EXTRACTOR_FACTORY: Callable[[float], Extractor] = build_extractor


@dataclass(frozen=True)
class ChannelParams:
    url: str
    max_videos: int = DEFAULT_CHANNEL_VIDEOS
    release: bool | int = True


def parse_channel_params(params: dict[str, Any]) -> ChannelParams:
    """The `backfill.channel` params; raises ValueError naming what is wrong."""
    unknown = sorted(set(params) - CHANNEL_PARAMS)
    if unknown:
        raise ValueError(f"unknown parameter(s) for backfill.channel: {', '.join(unknown)}")
    url = params.get("url")
    if not isinstance(url, str) or not is_channel_url(url):
        raise ValueError(f"url must be a YouTube channel or playlist URL, not {url!r}")
    max_videos = params.get("max_videos", DEFAULT_CHANNEL_VIDEOS)
    if (
        isinstance(max_videos, bool)
        or not isinstance(max_videos, int)
        or not 1 <= max_videos <= MAX_CHANNEL_VIDEOS
    ):
        raise ValueError(
            f"max_videos must be a whole number from 1 to {MAX_CHANNEL_VIDEOS}, not {max_videos!r}"
        )
    release = parse_release(params)
    return ChannelParams(url=url, max_videos=max_videos, release=release)


def handle_backfill_channel(ctx: HandlerContext, job: Job) -> HandlerResult:
    """List one channel through the gate and store its unknown videos as pending; release (by default)
    up to that daily cap. A closed gate is `Defer` (to the gate's time); offline, a block, a listing error
    or a listing too big for the gap rule is `Fail` and stores nothing."""
    try:
        params = parse_channel_params(dict(job.params or {}))
    except ValueError as e:
        return Fail(str(e))
    tag = f"backfill.channel {job.id}"
    access = ctx.services.youtube
    if access is None:
        return Fail("no YouTube access in the services: a channel is listed only through the gate")
    if access.offline:
        return Fail("YOUTUBE_OFFLINE is on: the channel listing calls YouTube, nothing was listed")
    too_big = listing_fit(
        params.max_videos,
        ctx.settings.youtube_min_gap_s,
        ctx.settings.youtube_request_delay_s,
        option="max_videos",
    )
    if too_big is not None:
        return Fail(too_big)
    extractor = EXTRACTOR_FACTORY(ctx.settings.youtube_request_delay_s)
    target = listing_url(params.url)
    try:
        ids = access.call(
            lambda: list_channel(target, max_videos=params.max_videos, extractor=extractor), wait=False
        )
    except FactsDeferred as e:
        if isinstance(
            e.__cause__, ChannelListingError
        ):  # YouTube blocked this very listing (breaker now open)
            log.error("%s: blocked during the listing: %s", tag, e)
            return Fail(f"{e.__cause__}: {e}")
        # the gap, the breaker or the gate's database: wait for the gate
        until = (
            datetime.fromtimestamp(e.until, UTC)
            if e.until is not None
            else ctx.clock() + timedelta(seconds=DEFER_FALLBACK_S)
        )
        log.info("%s: %s; the listing waits until %s", tag, e, until.isoformat(timespec="seconds"))
        return Defer(run_after=until, reason=str(e))
    except (FactsUnavailable, ChannelListingError) as e:
        log.error("%s: %s", tag, e)
        return Fail(str(e))
    now = ctx.clock()
    released: list[str] = []
    repaired: list[str] = []
    with session_scope(ctx.engine) as session:
        known, new = store_listing(session, ids, params.url, ctx.ideas, ctx.docs, now)
        log.info("%s: listed %d, known %d, new %d", tag, len(ids), known, new)
        cap = release_cap(params.release, ctx.settings.backfill_daily_limit)
        if cap > 0:
            allowance, outcome = release_within(session, ctx.ideas, cap, now, only=ids)
            released, repaired = outcome.released, outcome.repaired
            log.info(
                "%s: allowance %d, released %d, repaired %d", tag, allowance, len(released), len(repaired)
            )
        still_pending = store.counts(session)["pending"]
    return Done(
        {
            "url": params.url,
            "listed": len(ids),
            "known": known,
            "new": new,
            "released": released,
            "repaired": repaired,
            "still_pending": still_pending,
        }
    )
