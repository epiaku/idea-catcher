"""How the pipeline gets the facts of a video: saved facts first, then the gate, then YouTube.

saved facts?  -> use them, no call at all
offline?      -> no call (development and tests)
gate allows?  -> one paced fetch, and the result is saved
gate says no  -> not an error: "try later" (the document waits)
YouTube says no (a 429, a bot check) -> open the breaker, and "try later"
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from catcher.core.config import Settings
from catcher.modules.youtube.cache import FactsCache
from catcher.modules.youtube.facts import (
    FactsDeferred,
    FactsFetcher,
    FactsUnavailable,
    FetchSkipped,
    YoutubeFacts,
    fetch_facts,
    is_gone_for_good,
)
from catcher.modules.youtube.gate import Wait, YoutubeGate, is_block_error

log = logging.getLogger("catcher.youtube")


class YoutubeAccess:
    def __init__(
        self,
        fetch: FactsFetcher,
        gate: YoutubeGate,
        *,
        offline: bool = False,
        negative_ttl_s: float = 24 * 3600.0,
        wait_max_s: float = 1800.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.fetch = fetch
        self.gate = gate
        self.offline = offline
        self.negative_ttl_s = negative_ttl_s
        self.wait_max_s = wait_max_s
        self.clock = clock
        self.sleep = sleep

    def cache(self, facts_dir: Path | None) -> FactsCache | None:
        return (
            FactsCache(facts_dir, negative_ttl_s=self.negative_ttl_s, clock=self.clock) if facts_dir else None
        )

    def wait_needed(
        self, video_id: str, *, facts_dir: Path | None, refresh: bool = False, wait: bool = False
    ) -> Wait | None:
        """Must this document wait? None means go ahead: saved facts, an open gate, or (with `wait`) a gap
        short enough to sleep through inside the run.

        Read-only. The run asks this **before** it starts work on a document, so a clip that has to wait just
        stays in `inbox/` and the next run picks it up, with no requeue.
        """
        cache = self.cache(facts_dir)
        if not refresh and cache is not None and cache.get(video_id) is not None:
            return None
        if self.offline:
            return None  # get() explains it
        pending = self.gate.peek()
        if (
            pending is not None
            and wait
            and not pending.blocked
            and pending.until - self.clock() <= self.wait_max_s
        ):
            return None  # get() sleeps until the gap has passed
        return pending

    def get(
        self,
        video_id: str,
        *,
        facts_dir: Path | None,
        refresh: bool = False,
        write_cache: bool = True,
        wait: bool = False,
        fetch_allowed: bool = True,
    ) -> YoutubeFacts:
        """The facts of a video. With `fetch_allowed=False` (a dry run) only saved facts are used: nothing is
        asked of YouTube and the gate is not touched, so a dry run never costs a request."""
        cache = self.cache(facts_dir)
        if not refresh and cache is not None:
            saved = cache.get(video_id)
            if saved is not None:
                log.info("%s: using the saved YouTube facts (no call to YouTube)", video_id)
                return saved
        if self.offline:
            raise FactsUnavailable(f"YOUTUBE_OFFLINE is on and there are no saved facts for {video_id}")
        if not fetch_allowed:
            raise FetchSkipped(f"no saved facts for {video_id}: a dry run does not call YouTube")
        self._pass_the_gate(wait)
        started = self.clock()
        try:
            facts = self.fetch(video_id)
        except Exception as e:
            if is_block_error(e):
                until = self.gate.record_block(started)
                raise FactsDeferred(Wait(until, blocked=True).message(self.clock())) from e
            if is_gone_for_good(e):
                self._remember_gone(video_id, cache, write_cache, e)
            raise
        self.gate.record_success(started)
        if write_cache and cache is not None:
            cache.put(facts)
        return facts

    def _remember_gone(
        self, video_id: str, cache: FactsCache | None, write_cache: bool, error: Exception
    ) -> None:
        """A private or removed video: save that, so a requeue does not ask YouTube again for a day."""
        if not write_cache or cache is None:
            return
        moment = datetime.fromtimestamp(self.clock(), UTC)
        cache.put(
            YoutubeFacts(
                video_id=video_id,
                url=f"https://www.youtube.com/watch?v={video_id}",
                fetched_at=moment.date().isoformat(),
                fetched_utc=moment.isoformat(timespec="seconds"),
                unavailable_reason=str(error)[:300],
            )
        )

    def _pass_the_gate(self, wait: bool) -> None:
        while True:
            blocked_by = self.gate.reserve()
            if blocked_by is None:
                return
            now = self.clock()
            seconds = blocked_by.until - now
            if blocked_by.blocked or not wait or seconds > self.wait_max_s:
                raise FactsDeferred(blocked_by.message(now))
            log.info("%s: waiting %d s for the next YouTube call", blocked_by.message(now), int(seconds))
            self.sleep(max(0.0, seconds) + 1.0)


def build_access(settings: Settings) -> YoutubeAccess:
    """The real thing, from the settings (`YOUTUBE_*` in `.env`)."""
    languages = settings.transcript_language_list

    def fetch(video_id: str) -> YoutubeFacts:
        return fetch_facts(
            video_id,
            languages=languages,
            request_delay_s=settings.youtube_request_delay_s,
            skip_manifests=settings.youtube_skip_manifests,
        )

    gate = YoutubeGate(
        settings.catcher_state_dir,
        min_gap_s=settings.youtube_min_gap_s,
        jitter_s=settings.youtube_gap_jitter_s,
        block_hours=settings.youtube_block_hours,
    )
    return YoutubeAccess(
        fetch,
        gate,
        offline=settings.youtube_offline,
        negative_ttl_s=settings.youtube_negative_ttl_h * 3600,
        wait_max_s=settings.youtube_wait_max_s,
    )
