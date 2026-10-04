"""How the pipeline gets the facts of a video: saved facts first, then the gate, then YouTube.

saved facts?  -> use them, no call at all
offline?      -> no call (development and tests)
gate allows?  -> one paced fetch, and the result is saved
gate says no  -> not an error: "try later" (the document waits)
YouTube says no (a 429, a bot check) -> open the breaker, and "try later"
gate unavailable (the database of the Postgres gate) -> no call, and "try later" in GATE_RETRY_S
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
from catcher.modules.youtube.gate import Gate, GateUnavailable, Wait, YoutubeGate, is_block_error

log = logging.getLogger("catcher.youtube")

GATE_RETRY_S = 60  # the gate cannot read or write its row (the database): ask again in a minute


def _short(error: Exception, limit: int = 200) -> str:
    """The first line of an error, cut short: a database error carries its SQL and a link on later lines."""
    lines = str(error).strip().splitlines()
    first = lines[0] if lines else type(error).__name__
    return first if len(first) <= limit else first[: limit - 3] + "..."


class YoutubeAccess:
    def __init__(
        self,
        fetch: FactsFetcher,
        gate: Gate,
        *,
        offline: bool = False,
        negative_ttl_s: float = 24 * 3600.0,
        wait_max_s: float = 1800.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        unrecorded_block_s: float = 6 * 3600.0,
    ) -> None:
        self.fetch = fetch
        self.gate = gate
        self.offline = offline
        self.negative_ttl_s = negative_ttl_s
        self.wait_max_s = wait_max_s
        self.clock = clock
        self.sleep = sleep
        # A 429 the gate could not record (its database was unavailable): no call before this time, kept here
        # in memory because the gate does not know it. `build_access` sets the first breaker step.
        self.unrecorded_block_s = unrecorded_block_s
        self.unrecorded_until = 0.0
        # The start time of the fetch that got that 429: the block is recorded in the gate as soon as the
        # gate answers again (before any other gate call), so a restart, `catcher youtube gate` and the queue
        # see it.
        self.pending_block_started: float | None = None

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
        self._record_pending_block()
        if self.clock() < self.unrecorded_until:
            return Wait(self.unrecorded_until, blocked=True)
        try:
            pending = self.gate.peek()
        except GateUnavailable as e:
            # Closed, never open: the document waits a minute, like a gap. Only the Stage A run asks this,
            # and its file gate never raises GateUnavailable; a Wait (not FactsDeferred) keeps the contract.
            return Wait(self._retry_at(e), blocked=False)
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
            raise FetchSkipped(f"no saved facts for {video_id}: fetching from YouTube is not allowed here")
        self._pass_the_gate(wait)
        started = self.clock()
        try:
            facts = self.fetch(video_id)
        except Exception as e:
            if is_block_error(e):
                raise self._blocked(started) from e
            if is_gone_for_good(e):
                self._remember_gone(video_id, cache, write_cache, e)
            raise
        try:
            self.gate.record_success(started)
        except GateUnavailable as e:  # the facts are good: keep them; the breaker closes after the next fetch
            log.error("%s: the YouTube gate could not record a fetch that worked: %s", video_id, _short(e))
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

    def _retry_at(self, error: GateUnavailable) -> float:
        """The gate cannot say (its database is down, a lock timed out): no fetch, ask again in a minute."""
        log.error("the YouTube gate is unavailable, no call to YouTube: %s", _short(error))
        return self.clock() + GATE_RETRY_S

    def _gate_unavailable(self, error: GateUnavailable) -> FactsDeferred:
        return FactsDeferred(f"YouTube gate unavailable: {_short(error)}", self._retry_at(error))

    def _blocked(self, started: float) -> FactsDeferred:
        """YouTube said no: open the breaker. When the gate cannot record that, the block is real news all the
        same: this access makes no call for `unrecorded_block_s` (the first breaker step, not the one-minute
        retry of an unavailable gate), so the next document does not call YouTube during the block."""
        try:
            until = self.gate.record_block(started)
        except GateUnavailable as e:
            now = self.clock()
            until = self.unrecorded_until = max(self.unrecorded_until, now + self.unrecorded_block_s)
            self.pending_block_started = started
            log.error(
                "YouTube is blocking us, and the gate could not record the block (%s): no call for %g hours",
                _short(e),
                (until - now) / 3600,
            )
            return FactsDeferred(Wait(until, blocked=True).message(now), until)
        return FactsDeferred(Wait(until, blocked=True).message(self.clock()), until)

    def _record_pending_block(self) -> None:
        """A 429 the gate could not record: try again now, before anything else asks the gate. The in-memory
        hold stays as it is (the recorded block is at least as long); only the pending record is cleared."""
        if self.pending_block_started is None:
            return
        try:
            until = self.gate.record_block(self.pending_block_started)
        except GateUnavailable as e:
            log.error("the YouTube gate is still unavailable, the block stays in memory only: %s", _short(e))
            return
        self.pending_block_started = None
        log.warning(
            "the YouTube gate has now recorded the block it could not record before: %s",
            Wait(until, blocked=True).message(self.clock()),
        )

    def _pass_the_gate(self, wait: bool) -> None:
        self._record_pending_block()
        now = self.clock()
        if now < self.unrecorded_until:  # a block the gate does not know about: no call, not even a reserve
            raise FactsDeferred(Wait(self.unrecorded_until, blocked=True).message(now), self.unrecorded_until)
        while True:
            try:
                blocked_by = self.gate.reserve()
            except GateUnavailable as e:
                raise self._gate_unavailable(e) from e
            if blocked_by is None:
                return
            now = self.clock()
            seconds = blocked_by.until - now
            if blocked_by.blocked or not wait or seconds > self.wait_max_s:
                raise FactsDeferred(blocked_by.message(now), blocked_by.until)
            log.info("%s: waiting %d s for the next YouTube call", blocked_by.message(now), int(seconds))
            self.sleep(max(0.0, seconds) + 1.0)


def build_access(
    settings: Settings, gate: Gate | None = None, *, clock: Callable[[], float] = time.time
) -> YoutubeAccess:
    """The real thing, from the settings (`YOUTUBE_*` in `.env`). A given `gate` (the worker's Postgres gate)
    replaces the file gate in CATCHER_STATE_DIR, which the Stage A CLI keeps. `clock` (seconds since the
    epoch) is the access's own time, for its deferrals and a fetch's start (the worker passes its own)."""
    languages = settings.transcript_language_list

    def fetch(video_id: str) -> YoutubeFacts:
        return fetch_facts(
            video_id,
            languages=languages,
            request_delay_s=settings.youtube_request_delay_s,
            skip_manifests=settings.youtube_skip_manifests,
        )

    if gate is None:
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
        clock=clock,
        unrecorded_block_s=settings.youtube_block_hours * 3600,
    )
