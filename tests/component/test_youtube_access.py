from pathlib import Path

import pytest

from catcher.modules.youtube.access import YoutubeAccess
from catcher.modules.youtube.cache import FactsCache
from catcher.modules.youtube.facts import FactsDeferred, FactsUnavailable, Segment, YoutubeFacts
from catcher.modules.youtube.gate import YoutubeGate

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
