import logging
import subprocess
import sys
from pathlib import Path

import pytest

from catcher.core.config import Settings
from catcher.modules.pipeline.process import default_services
from catcher.modules.youtube.access import GATE_RETRY_S, YoutubeAccess, build_access
from catcher.modules.youtube.cache import FactsCache
from catcher.modules.youtube.facts import FactsDeferred, FactsUnavailable, Segment, YoutubeFacts
from catcher.modules.youtube.gate import Gate, GateUnavailable, Wait, YoutubeGate

VID = "nGVZS_wUDGM"


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class Fetcher:
    """Counts the calls to 'YouTube'."""

    def __init__(self, *, transcript=True, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.transcript = transcript
        self.error = error

    def __call__(self, video_id: str) -> YoutubeFacts:
        self.calls.append(video_id)
        if self.error:
            raise self.error
        return YoutubeFacts(
            video_id=video_id,
            url="u",
            title="T",
            transcript=[Segment(start_s=0, text="hi")] if self.transcript else None,
            fetched_at="2026-10-01",
            fetched_utc="2023-11-14T22:13:20+00:00",  # = the fake clock's 1_700_000_000
        )


class HttpError(Exception):
    status = 429


def make(tmp_path, fetch, clock=None, **kw):
    clock = clock or Clock()
    gate = YoutubeGate(tmp_path / "state", min_gap_s=600, jitter_s=0, block_hours=6, clock=clock)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.now += seconds

    return YoutubeAccess(fetch, gate, clock=clock, sleep=sleep, **kw), clock, sleeps


def test_a_new_video_is_fetched_once_and_saved(tmp_path):
    fetch = Fetcher()
    access, _, _ = make(tmp_path, fetch)
    facts = access.get(VID, facts_dir=tmp_path / "facts")
    assert facts.title == "T" and fetch.calls == [VID]
    assert (tmp_path / "facts" / f"{VID}.json").exists()


def test_the_saved_facts_are_used_and_youtube_is_not_called_again(tmp_path):
    fetch = Fetcher()
    access, clock, _ = make(tmp_path, fetch)
    access.get(VID, facts_dir=tmp_path / "facts")
    clock.now += 10 * 3600  # long after the gap
    for _ in range(3):  # a retry, a requeue, a rerun
        assert access.get(VID, facts_dir=tmp_path / "facts").title == "T"
    assert fetch.calls == [VID]


def test_refresh_fetches_again_and_replaces_the_saved_facts(tmp_path):
    fetch = Fetcher()
    access, clock, _ = make(tmp_path, fetch)
    access.get(VID, facts_dir=tmp_path / "facts")
    clock.now += 700
    access.get(VID, facts_dir=tmp_path / "facts", refresh=True)
    assert fetch.calls == [VID, VID]


def test_a_second_video_inside_the_gap_is_deferred_not_fetched(tmp_path):
    fetch = Fetcher()
    access, _, _ = make(tmp_path, fetch)
    access.get("AAAAAAAAAAA", facts_dir=tmp_path / "facts")
    with pytest.raises(FactsDeferred, match="next call allowed at"):
        access.get("BBBBBBBBBBB", facts_dir=tmp_path / "facts")
    assert fetch.calls == ["AAAAAAAAAAA"]


def test_wait_needed_is_read_only_and_says_when_a_fetch_must_wait(tmp_path):
    fetch = Fetcher()
    access, clock, _ = make(tmp_path, fetch)
    facts_dir = tmp_path / "facts"
    assert access.wait_needed("AAAAAAAAAAA", facts_dir=facts_dir) is None
    assert access.wait_needed("AAAAAAAAAAA", facts_dir=facts_dir) is None  # asking changes nothing
    access.get("AAAAAAAAAAA", facts_dir=facts_dir)
    waiting = access.wait_needed("BBBBBBBBBBB", facts_dir=facts_dir)
    assert waiting is not None and not waiting.blocked and waiting.until == clock.now + 600
    assert access.wait_needed("AAAAAAAAAAA", facts_dir=facts_dir) is None  # saved facts never wait


def test_wait_mode_sleeps_until_the_gap_has_passed_and_then_fetches(tmp_path):
    fetch = Fetcher()
    access, clock, sleeps = make(tmp_path, fetch)
    access.get("AAAAAAAAAAA", facts_dir=tmp_path / "facts")
    access.get("BBBBBBBBBBB", facts_dir=tmp_path / "facts", wait=True)
    assert fetch.calls == ["AAAAAAAAAAA", "BBBBBBBBBBB"] and len(sleeps) == 1 and 599 < sleeps[0] < 603


def test_wait_mode_does_not_wait_longer_than_the_limit(tmp_path):
    fetch = Fetcher()
    access, _, sleeps = make(tmp_path, fetch, wait_max_s=60)
    access.get("AAAAAAAAAAA", facts_dir=tmp_path / "facts")
    with pytest.raises(FactsDeferred):
        access.get("BBBBBBBBBBB", facts_dir=tmp_path / "facts", wait=True)
    assert sleeps == []


def test_a_429_opens_the_breaker_and_nothing_is_called_until_it_ends(tmp_path):
    fetch = Fetcher(error=HttpError("429"))
    access, clock, _ = make(tmp_path, fetch)
    with pytest.raises(FactsDeferred, match="YouTube blocked until"):
        access.get("AAAAAAAAAAA", facts_dir=tmp_path / "facts")
    assert fetch.calls == ["AAAAAAAAAAA"]
    clock.now += 3600  # an hour later, and the gap is long gone, but the breaker is open
    assert access.wait_needed("BBBBBBBBBBB", facts_dir=tmp_path / "facts").blocked  # type: ignore[union-attr]
    with pytest.raises(FactsDeferred, match="blocked"):
        access.get("BBBBBBBBBBB", facts_dir=tmp_path / "facts", wait=True)  # waiting does not help
    assert fetch.calls == ["AAAAAAAAAAA"]  # still the one call


def test_a_fetch_that_works_after_a_block_closes_the_breaker(tmp_path):
    fetch = Fetcher(error=HttpError("429"))
    access, clock, _ = make(tmp_path, fetch)
    with pytest.raises(FactsDeferred):
        access.get("AAAAAAAAAAA", facts_dir=tmp_path / "facts")
    clock.now += 6 * 3600 + 700
    fetch.error = None
    access.get("AAAAAAAAAAA", facts_dir=tmp_path / "facts")  # the probe works
    assert access.wait_needed("BBBBBBBBBBB", facts_dir=tmp_path / "facts") is not None  # the gap, not a block
    assert not access.wait_needed("BBBBBBBBBBB", facts_dir=tmp_path / "facts").blocked  # type: ignore[union-attr]


def test_another_failure_is_not_a_block(tmp_path):
    fetch = Fetcher(error=FactsUnavailable("Video unavailable"))
    access, _, _ = make(tmp_path, fetch)
    with pytest.raises(FactsUnavailable) as info:
        access.get("AAAAAAAAAAA", facts_dir=tmp_path / "facts")
    assert not isinstance(info.value, FactsDeferred)
    assert access.gate.peek() is not None and not access.gate.peek().blocked  # type: ignore[union-attr]


def test_offline_never_calls_youtube(tmp_path):
    fetch = Fetcher()
    access, _, _ = make(tmp_path, fetch, offline=True)
    with pytest.raises(FactsUnavailable, match="YOUTUBE_OFFLINE"):
        access.get(VID, facts_dir=tmp_path / "facts")
    FactsCache(tmp_path / "facts").put(Fetcher()(VID))
    assert access.get(VID, facts_dir=tmp_path / "facts").title == "T"  # saved facts still work offline
    assert fetch.calls == []


def test_a_video_without_captions_is_asked_again_only_after_a_day(tmp_path):
    fetch = Fetcher(transcript=False)
    access, clock, _ = make(tmp_path, fetch)
    access.get(VID, facts_dir=tmp_path / "facts")
    clock.now += 700
    access.get(VID, facts_dir=tmp_path / "facts")  # inside the day: remembered
    assert fetch.calls == [VID]
    clock.now += 24 * 3600
    access.get(VID, facts_dir=tmp_path / "facts")  # a day later: asked again
    assert fetch.calls == [VID, VID]


def test_dry_run_style_calls_do_not_write_the_cache(tmp_path):
    access, _, _ = make(tmp_path, Fetcher())
    access.get(VID, facts_dir=tmp_path / "facts", write_cache=False)
    assert not (tmp_path / "facts").exists()


def test_without_a_facts_folder_there_is_no_cache_but_the_gate_still_applies(tmp_path):
    fetch = Fetcher()
    access, _, _ = make(tmp_path, fetch)
    access.get("AAAAAAAAAAA", facts_dir=None)
    with pytest.raises(FactsDeferred):
        access.get("AAAAAAAAAAA", facts_dir=None)
    assert fetch.calls == ["AAAAAAAAAAA"]


def test_the_cache_refuses_a_bad_video_id(tmp_path):
    with pytest.raises(ValueError):
        FactsCache(tmp_path).path("../../etc/passwd")
    assert isinstance(FactsCache(tmp_path).path(VID), Path)


def test_a_dry_run_uses_saved_facts_but_never_asks_youtube_or_the_gate(tmp_path):
    from catcher.modules.youtube.facts import FetchSkipped

    fetch = Fetcher()
    access, _, _ = make(tmp_path, fetch)
    with pytest.raises(FetchSkipped):
        access.get(VID, facts_dir=tmp_path / "facts", fetch_allowed=False)
    assert fetch.calls == [] and not access.gate.state_file.exists()
    access.get(VID, facts_dir=tmp_path / "facts")  # a real fetch saves the facts ...
    assert (
        access.get(VID, facts_dir=tmp_path / "facts", fetch_allowed=False).title == "T"
    )  # ... a dry run reads them


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [youtube] AAAAAAAAAAA: Private video. Sign in",
        "Video unavailable. This video has been removed",
    ],
)
def test_a_video_that_is_gone_for_good_is_remembered_so_a_requeue_does_not_ask_again(tmp_path, message):
    fetch = Fetcher(error=FactsUnavailable(f"yt-dlp failed: {message}"))
    access, clock, _ = make(tmp_path, fetch)
    with pytest.raises(FactsUnavailable):
        access.get(VID, facts_dir=tmp_path / "facts")
    saved = FactsCache(tmp_path / "facts", clock=clock).get(VID)
    assert saved is not None and saved.unavailable_reason and "yt-dlp failed" in saved.unavailable_reason
    clock.now += 700  # the gap has passed, but the answer is still saved
    assert access.get(VID, facts_dir=tmp_path / "facts").unavailable_reason == saved.unavailable_reason
    assert fetch.calls == [VID]
    clock.now += 25 * 3600  # a day later it may be asked again
    with pytest.raises(FactsUnavailable):
        access.get(VID, facts_dir=tmp_path / "facts")
    assert fetch.calls == [VID, VID]


def test_an_ordinary_failure_is_not_remembered(tmp_path):
    fetch = Fetcher(error=FactsUnavailable("yt-dlp failed: connection reset"))
    access, clock, _ = make(tmp_path, fetch)
    with pytest.raises(FactsUnavailable):
        access.get(VID, facts_dir=tmp_path / "facts")
    assert not (tmp_path / "facts").exists()
    clock.now += 700
    with pytest.raises(FactsUnavailable):
        access.get(VID, facts_dir=tmp_path / "facts")
    assert fetch.calls == [VID, VID]  # asked again: it may well work now


def test_a_dry_run_does_not_save_a_gone_video_either(tmp_path):
    fetch = Fetcher(error=FactsUnavailable("Private video"))
    access, _, _ = make(tmp_path, fetch)
    with pytest.raises(FactsUnavailable):
        access.get(VID, facts_dir=tmp_path / "facts", write_cache=False)
    assert not (tmp_path / "facts").exists()


class FakeGate:
    """A gate that follows the Gate protocol and keeps its state in memory: no files."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.blocked_until = 0.0
        self.events: list[str] = []

    def _wait(self) -> Wait | None:
        if self.clock.now < self.blocked_until:
            return Wait(self.blocked_until, blocked=True)
        return None

    def peek(self) -> Wait | None:
        return self._wait()

    def reserve(self) -> Wait | None:
        wait = self._wait()
        if wait is None:
            self.events.append("reserve")
        return wait

    def record_success(self, started_at: float | None = None) -> None:
        self.events.append("success")

    def record_block(self, started_at: float | None = None) -> float:
        self.events.append("block")
        self.blocked_until = self.clock.now + 6 * 3600
        return self.blocked_until


def test_access_works_with_any_gate_that_follows_the_protocol(tmp_path):
    clock = Clock()
    gate = FakeGate(clock)
    as_protocol: Gate = gate  # the fake follows the protocol
    fetch = Fetcher()
    access = YoutubeAccess(fetch, as_protocol, clock=clock)

    assert access.get(VID, facts_dir=None).title == "T"  # success
    assert fetch.calls == [VID]

    fetch.error = HttpError("too many requests")  # a 429 opens the breaker
    with pytest.raises(FactsDeferred, match="blocked until"):
        access.get("BBBBBBBBBBB", facts_dir=None)
    assert fetch.calls == [VID, "BBBBBBBBBBB"]

    with pytest.raises(FactsDeferred, match="blocked until"):  # a closed gate: no call at all
        access.get("CCCCCCCCCCC", facts_dir=None)
    assert fetch.calls == [VID, "BBBBBBBBBBB"]
    assert access.wait_needed("CCCCCCCCCCC", facts_dir=None) is not None

    assert gate.events == ["reserve", "success", "reserve", "block"]
    assert list(tmp_path.iterdir()) == []  # nothing written anywhere


def test_facts_deferred_carries_the_gate_time(tmp_path):
    fetch = Fetcher()
    access, clock, _ = make(tmp_path / "gap", fetch)
    access.get("AAAAAAAAAAA", facts_dir=tmp_path / "facts")
    with pytest.raises(FactsDeferred) as gap:
        access.get("BBBBBBBBBBB", facts_dir=tmp_path / "facts")
    assert gap.value.until == clock.now + 600  # the gate's next allowed time, in epoch seconds

    blocked, block_clock, _ = make(tmp_path / "block", Fetcher(error=HttpError("429")))
    with pytest.raises(FactsDeferred) as block:
        blocked.get("AAAAAAAAAAA", facts_dir=tmp_path / "facts-b")
    assert block.value.until == block_clock.now + 6 * 3600  # the end of the block
    with pytest.raises(FactsDeferred) as still:  # the open breaker, seen at the gate
        blocked.get("BBBBBBBBBBB", facts_dir=tmp_path / "facts-b")
    assert still.value.until == block.value.until

    assert FactsDeferred("later").until is None  # the old way to raise it still works


class UnavailableGate(FakeGate):
    """A gate whose database is down: every call named in `failing` raises GateUnavailable."""

    def __init__(self, clock: Clock, *failing: str) -> None:
        super().__init__(clock)
        self.failing = set(failing)

    def _maybe_fail(self, call: str) -> None:
        if call in self.failing:
            self.events.append(f"{call} failed")
            raise GateUnavailable("the YouTube gate row 'youtube' is unavailable: connection refused")

    def peek(self) -> Wait | None:
        self._maybe_fail("peek")
        return super().peek()

    def reserve(self) -> Wait | None:
        self._maybe_fail("reserve")
        return super().reserve()

    def record_success(self, started_at: float | None = None) -> None:
        self._maybe_fail("record_success")
        super().record_success(started_at)

    def record_block(self, started_at: float | None = None) -> float:
        self._maybe_fail("record_block")
        return super().record_block(started_at)


def test_a_gate_that_is_unavailable_defers_and_never_fetches(tmp_path):
    clock = Clock()
    fetch = Fetcher()
    access = YoutubeAccess(fetch, UnavailableGate(clock, "peek", "reserve"), clock=clock)

    with pytest.raises(FactsDeferred, match="YouTube gate unavailable") as deferred:
        access.get(VID, facts_dir=tmp_path / "facts")
    assert deferred.value.until == clock.now + GATE_RETRY_S == clock.now + 60
    assert fetch.calls == []  # a database hiccup never opens the gate

    wait = access.wait_needed(VID, facts_dir=tmp_path / "facts")  # the read-only question: wait a minute
    assert wait is not None and wait.until == clock.now + 60
    assert fetch.calls == []


def test_a_block_the_gate_cannot_record_defers_for_a_whole_breaker_step(tmp_path, caplog):
    """A 429 is real news: when the gate cannot record it, the fetch waits the first breaker step (not the
    one-minute retry of an unavailable gate), so the next document does not call YouTube during the block."""
    clock = Clock()
    fetch = Fetcher(error=HttpError("HTTP Error 429: Too Many Requests"))
    gate = UnavailableGate(clock, "record_block")
    block_s = 6 * 3600
    access = YoutubeAccess(fetch, gate, clock=clock, unrecorded_block_s=block_s)

    with caplog.at_level(logging.ERROR, logger="catcher.youtube"), pytest.raises(FactsDeferred) as deferred:
        access.get(VID, facts_dir=tmp_path / "facts")
    assert deferred.value.until == clock.now + block_s
    assert fetch.calls == [VID]
    assert not (tmp_path / "facts").exists()  # nothing saved: the fetch did not work
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("could not record" in m for m in errors), errors

    clock.now += 3600  # an hour later, with a gate that would let a fetch through: still no call
    with pytest.raises(FactsDeferred, match="blocked until") as again:
        access.get("BBBBBBBBBBB", facts_dir=tmp_path / "facts")
    assert again.value.until == deferred.value.until
    wait = access.wait_needed("BBBBBBBBBBB", facts_dir=tmp_path / "facts")
    assert wait is not None and wait.blocked and wait.until == deferred.value.until
    assert fetch.calls == [VID]  # once, not twice

    clock.now = deferred.value.until  # the step has passed: the gate decides again
    fetch.error = None
    assert access.get("BBBBBBBBBBB", facts_dir=tmp_path / "facts").title == "T"
    assert fetch.calls == [VID, "BBBBBBBBBBB"]


def test_a_block_the_gate_could_not_record_reaches_the_gate_when_it_answers_again(tmp_path, caplog):
    """The in-memory hold is not enough: a restart forgets it. As soon as the gate answers again, the block is
    recorded there first (with the start time of the fetch that got the 429), before any other gate call."""
    clock = Clock()
    fetch = Fetcher(error=HttpError("HTTP Error 429: Too Many Requests"))
    gate = UnavailableGate(clock, "peek", "reserve", "record_block")
    access = YoutubeAccess(fetch, gate, clock=clock, unrecorded_block_s=6 * 3600)
    gate.failing = {"record_block"}  # the fetch passes the gate, gets the 429, and the database fails then
    started = clock.now
    with pytest.raises(FactsDeferred):
        access.get(VID, facts_dir=None)
    seen: list[float | None] = []
    original = gate.record_block

    def record_block(started_at: float | None = None) -> float:
        seen.append(started_at)
        return original(started_at)

    gate.record_block = record_block  # type: ignore[method-assign]

    gate.failing = {"peek", "reserve", "record_block"}  # still down: the block stays in memory only
    clock.now += 60
    with pytest.raises(FactsDeferred, match="blocked until"):
        access.get("BBBBBBBBBBB", facts_dir=None)
    assert access.wait_needed("BBBBBBBBBBB", facts_dir=None) is not None
    assert gate.blocked_until == 0.0

    gate.failing = set()  # the database is back: the next question records the block first
    clock.now += 60
    gate.events.clear()
    wait = access.wait_needed("BBBBBBBBBBB", facts_dir=None)
    assert wait is not None and wait.blocked
    assert gate.events[0] == "block" and seen[-1] == started
    assert gate.blocked_until == clock.now + 6 * 3600  # the gate itself now says blocked

    gate.events.clear()
    with pytest.raises(FactsDeferred, match="blocked until"):  # recorded once, not again
        access.get("BBBBBBBBBBB", facts_dir=None)
    assert "block" not in gate.events
    assert fetch.calls == [VID]

    restarted = YoutubeAccess(fetch, gate, clock=clock)  # a new process: no memory, the gate knows
    later = restarted.wait_needed("CCCCCCCCCCC", facts_dir=None)
    assert later is not None and later.blocked
    with pytest.raises(FactsDeferred, match="blocked until"):
        restarted.get("CCCCCCCCCCC", facts_dir=None)
    assert fetch.calls == [VID]


def test_a_pending_block_is_recorded_before_the_reserve(tmp_path):
    clock = Clock()
    fetch = Fetcher(error=HttpError("HTTP Error 429: Too Many Requests"))
    gate = UnavailableGate(clock, "record_block")
    access = YoutubeAccess(fetch, gate, clock=clock, unrecorded_block_s=6 * 3600)
    with pytest.raises(FactsDeferred):
        access.get(VID, facts_dir=None)
    gate.failing = set()
    gate.events.clear()
    with pytest.raises(FactsDeferred, match="blocked until"):
        access.get("BBBBBBBBBBB", facts_dir=None)
    assert gate.events == ["block"]  # recorded first; the hold then answers, no reserve and no fetch
    assert fetch.calls == [VID]


def test_a_success_the_gate_cannot_record_still_returns_the_facts(tmp_path, caplog):
    clock = Clock()
    fetch = Fetcher()
    access = YoutubeAccess(fetch, UnavailableGate(clock, "record_success"), clock=clock)

    with caplog.at_level(logging.ERROR, logger="catcher.youtube"):
        assert access.get(VID, facts_dir=tmp_path / "facts").title == "T"
    assert (tmp_path / "facts" / f"{VID}.json").exists()  # a fetch that worked is never thrown away
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_build_access_uses_a_given_gate(tmp_path):
    settings = Settings(catcher_state_dir=tmp_path / "state")
    gate = FakeGate(Clock())

    assert build_access(settings, gate).gate is gate
    assert build_access(settings, gate).unrecorded_block_s == settings.youtube_block_hours * 3600
    assert isinstance(build_access(settings).gate, YoutubeGate)  # the Stage A CLI keeps the file gate
    assert default_services(settings, gate=gate).youtube.gate is gate  # type: ignore[union-attr]


def test_the_access_layer_imports_no_sqlalchemy():
    """The Stage A CLI keeps the file gate and needs no database: importing the access layer (which handles
    `GateUnavailable`) must not pull in SQLAlchemy."""
    code = "import sys, catcher.modules.youtube.access; print('sqlalchemy' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "False"
