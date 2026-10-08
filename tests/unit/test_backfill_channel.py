"""The channel listing: a fake extractor only (yt-dlp is never called), and the access's gate for one call."""

import socket
from typing import Any

import blocknet
import pytest
from memory_gate import InMemoryGate

from catcher.modules.backfill import channel
from catcher.modules.backfill.channel import (
    ChannelListingError,
    is_channel_url,
    list_channel,
    listing_url,
    ydl_options,
)
from catcher.modules.youtube.access import YoutubeAccess
from catcher.modules.youtube.facts import FactsDeferred, FactsUnavailable

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"
URL = "https://www.youtube.com/@small/videos"


class FakeExtractor:
    def __init__(self, result: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.result = result or {"entries": []}
        self.error = error
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


def _entry(vid: str) -> dict[str, Any]:
    return {"_type": "url", "id": vid, "url": f"https://www.youtube.com/watch?v={vid}"}


def test_list_channel_returns_valid_ids_in_order_without_duplicates() -> None:
    fake = FakeExtractor({"entries": [_entry(B), _entry(A), _entry(B), {"id": C, "url": C}, _entry(A)]})

    assert list_channel(URL, max_videos=10, extractor=fake) == [B, A, C]


def test_list_channel_passes_max_videos_and_the_request_delay_to_the_extractor() -> None:
    fake = FakeExtractor({"entries": [_entry(A), _entry(B), _entry(C)]})

    assert list_channel(URL, max_videos=2, extractor=fake) == [A, B]  # never more than asked
    assert fake.calls == [((URL,), {"max_videos": 2})]
    assert ydl_options(2, 12.5) == {
        "extract_flat": True,
        "playlistend": 2,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "sleep_interval_requests": 12.5,
        "retries": 1,
        "extractor_retries": 1,
    }
    options = ydl_options(5, 10.0)
    assert not any("cookie" in key or key in {"username", "password", "usenetrc"} for key in options)


def test_a_bad_entry_or_a_non_video_url_in_the_listing_is_skipped() -> None:
    entries = [
        None,
        "not a dict",
        {"id": "short"},
        {"id": "UCabcdefghijklmnopqrstuv", "url": "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv"},
        {"id": A, "url": "https://www.youtube.com/playlist?list=PLx"},  # a playlist, whatever its id says
        {"url": "https://example.com/watch?v=" + B},
        {"id": "bad id with!", "url": None},
        _entry(C),
        {"_type": "playlist", "entries": [_entry(A), {"id": "x"}]},  # a tab of a channel: one level down
        {"url": f"https://www.youtube.com/shorts/{B}"},
    ]

    assert list_channel(URL, max_videos=50, extractor=FakeExtractor({"entries": entries})) == [C, A, B]
    assert list_channel(URL, max_videos=50, extractor=FakeExtractor({"title": "no entries"})) == []
    with pytest.raises(ChannelListingError):
        list_channel(URL, max_videos=50, extractor=lambda *a, **k: None)  # type: ignore[arg-type,return-value]


def test_an_extractor_error_raises_channel_listing_error_and_a_429_sets_the_flag() -> None:
    with pytest.raises(ChannelListingError) as plain:
        list_channel(
            URL, max_videos=5, extractor=FakeExtractor(error=RuntimeError("This channel does not exist"))
        )
    assert plain.value.blocked is False
    assert "does not exist" in str(plain.value)

    for text in ("HTTP Error 429: Too Many Requests", "Sign in to confirm you're not a bot"):
        with pytest.raises(ChannelListingError) as blocked:
            list_channel(URL, max_videos=5, extractor=FakeExtractor(error=RuntimeError(text)))
        assert blocked.value.blocked is True


def test_channel_urls_and_the_listing_url() -> None:
    good = [
        "https://www.youtube.com/@small",
        "https://youtube.com/@small/videos",
        "https://m.youtube.com/channel/UCabcdefghijklmnopqrstuv",
        "https://www.youtube.com/c/Name",
        "https://www.youtube.com/user/name",
        "https://www.youtube.com/playlist?list=PLabc",
    ]
    bad = [
        "https://www.youtube.com/watch?v=" + A,
        "https://youtu.be/" + A,
        "https://www.youtube.com/@",
        "https://www.youtube.com/channel/",
        "https://www.youtube.com/playlist",
        "https://evil.example/@small",
        "https://www.youtube.com.evil.example/@small",
        "ftp://www.youtube.com/@small",
        "@small",
        "",
    ]
    assert [u for u in good if not is_channel_url(u)] == []
    assert [u for u in bad if is_channel_url(u)] == []
    assert listing_url("https://www.youtube.com/@small") == "https://www.youtube.com/@small/videos"
    assert listing_url("https://www.youtube.com/@small/") == "https://www.youtube.com/@small/videos"
    assert listing_url("https://www.youtube.com/@small/shorts") == "https://www.youtube.com/@small/shorts"
    assert (
        listing_url("https://www.youtube.com/playlist?list=PLabc")
        == "https://www.youtube.com/playlist?list=PLabc"
    )


def test_the_network_guard_stays_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the module and building the default extractor opens no connection and calls no yt-dlp."""
    assert blocknet._originals, "the network guard is not installed"
    assert socket.socket.connect is not blocknet._originals["connect"]
    attempts = len(blocknet.ATTEMPTS)
    import yt_dlp

    def no_ydl(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("yt-dlp was called")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", no_ydl)

    extractor = channel.build_extractor(10.0)

    assert callable(extractor)  # built, never called
    assert len(blocknet.ATTEMPTS) == attempts


# ---- the access: one call that is not a video's facts goes through the same gate -------------------------


def _access(gate: InMemoryGate, *, offline: bool = False, now: float = 1_000_000.0) -> YoutubeAccess:
    def no_fetch(video_id: str) -> Any:
        raise AssertionError("no fetch here")

    return YoutubeAccess(no_fetch, gate, offline=offline, clock=lambda: now, sleep=lambda s: None)


def test_access_call_takes_one_slot_and_records_success() -> None:
    gate = InMemoryGate(min_gap_s=100, jitter_s=0, clock=lambda: 1_000_000.0)
    access = _access(gate)

    assert access.call(lambda: "done") == "done"

    assert gate.state.next_allowed_at == 1_000_100.0
    with pytest.raises(FactsDeferred):  # the gap: a second call right away is refused
        access.call(lambda: "again")


def test_access_call_with_a_block_opens_the_breaker_and_an_ordinary_error_does_not() -> None:
    gate = InMemoryGate(min_gap_s=0, jitter_s=0, clock=lambda: 1_000_000.0)
    access = _access(gate)

    def ordinary() -> None:
        raise ChannelListingError("no such channel")

    with pytest.raises(ChannelListingError):
        access.call(ordinary)
    assert gate.state.blocked_until == 0

    def blocked() -> None:
        raise ChannelListingError("HTTP Error 429: Too Many Requests", blocked=True)

    with pytest.raises(FactsDeferred, match="YouTube blocked until"):
        access.call(blocked)
    assert gate.state.blocked_until > 1_000_000.0
    assert access.wait_for_call() is not None and access.wait_for_call().blocked  # type: ignore[union-attr]


def test_access_call_offline_calls_nothing() -> None:
    gate = InMemoryGate(min_gap_s=100, jitter_s=0, clock=lambda: 1_000_000.0)
    called: list[int] = []

    with pytest.raises(FactsUnavailable, match="YOUTUBE_OFFLINE"):
        _access(gate, offline=True).call(lambda: called.append(1))

    assert called == []
    assert gate.state.next_allowed_at == 0
