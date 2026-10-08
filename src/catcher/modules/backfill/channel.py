"""The channel listing: the video ids of a YouTube channel or playlist, in one flat, paced yt-dlp call.

A listing is a call to YouTube like a fetch (several paced requests: one per page of about 30 videos), so the
caller passes it through the same gate (`YoutubeAccess.call`): one gate slot per channel, the gap, and the
breaker on a block. This module only does the listing; it asks no gate and stores nothing.

The extractor is injected: `build_extractor` wraps yt-dlp for the CLI, the tests pass a fake. yt-dlp is
imported only inside a call of the real extractor, so importing this module or building the extractor
touches nothing.
"""

from collections.abc import Callable
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from catcher.modules.youtube.gate import is_block_error
from catcher.modules.youtube.urls import YOUTUBE_HOSTS, host_of, video_id

Extractor = Callable[..., dict[str, Any]]

_CHANNEL_HOSTS = YOUTUBE_HOSTS - {"youtu.be"}  # youtu.be has video links only
_CHANNEL_PREFIXES = {"channel", "c", "user"}
_TABS = {"videos", "shorts", "streams", "live", "playlists", "featured"}


class ChannelListingError(Exception):
    """The listing failed. `blocked`: YouTube refused us (a 429, a bot check), so the caller opens the
    breaker."""

    def __init__(self, message: str, *, blocked: bool = False) -> None:
        super().__init__(message)
        self.blocked = blocked


def is_channel_url(url: str) -> bool:
    """A YouTube channel (`/@handle`, `/channel/ID`, `/c/name`, `/user/name`) or playlist (`/playlist?list=`)
    URL, on a YouTube host."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or host_of(url) not in _CHANNEL_HOSTS:
        return False
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return False
    if parts[0].startswith("@"):
        return len(parts[0]) > 1
    if parts[0] in _CHANNEL_PREFIXES:
        return len(parts) >= 2
    if parts == ["playlist"]:
        return bool(parse_qs(parsed.query).get("list"))
    return False


def listing_url(url: str) -> str:
    """The URL to list: a channel without a tab gets `/videos` (without it yt-dlp lists the tabs, not the
    videos); a playlist or a channel tab is kept as it is."""
    parsed = urlparse(url.strip())
    parts = [p for p in parsed.path.split("/") if p]
    if not parts or parts[0] == "playlist":
        return url.strip()
    base = 1 if parts[0].startswith("@") else 2
    if len(parts) > base and parts[base] in _TABS:
        return url.strip()
    return parsed._replace(path="/" + "/".join([*parts[:base], "videos"])).geturl()


def ydl_options(max_videos: int, request_delay_s: float) -> dict[str, Any]:
    """The yt-dlp options of a listing: flat (no video page is opened), at most `max_videos` entries, every
    request paced, one try (retrying while YouTube says no makes a block longer), no cookies, no login."""
    return {
        "extract_flat": True,
        "playlistend": max_videos,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "sleep_interval_requests": request_delay_s,
        "retries": 1,
        "extractor_retries": 1,
    }


def build_extractor(request_delay_s: float) -> Extractor:
    """The real extractor: `extract(url, max_videos=N)` runs one yt-dlp flat listing. Building it calls
    nothing; only the CLI builds it, and only after the gate gave it a slot does it call it."""

    def extract(url: str, *, max_videos: int) -> dict[str, Any]:
        import yt_dlp

        with yt_dlp.YoutubeDL(cast(Any, ydl_options(max_videos, request_delay_s))) as ydl:
            return cast(dict[str, Any], ydl.extract_info(url, download=False))

    return extract


def _entry_id(entry: object) -> str | None:
    """The video id of one flat entry, or None (a channel, a playlist, a damaged entry)."""
    if not isinstance(entry, dict):
        return None
    url, raw_id = entry.get("url"), entry.get("id")
    if isinstance(url, str) and url.strip() and url.strip() != raw_id:
        return video_id(url)  # a URL decides: a playlist or channel URL is not a video, whatever its id
    if isinstance(raw_id, str):
        return video_id(f"https://www.youtube.com/watch?v={raw_id}")  # the 11-character rule
    return None


def _entries(info: dict[str, Any]) -> list[object]:
    """The entries, a channel's tabs opened one level (each tab is a playlist of videos)."""
    found: list[object] = []
    for entry in info.get("entries") or []:
        nested = entry.get("entries") if isinstance(entry, dict) else None
        found.extend(nested if isinstance(nested, list) else [entry])
    return found


def list_channel(url: str, *, max_videos: int, extractor: Extractor) -> list[str]:
    """The video ids of a channel or playlist: in the listing's order, without duplicates, at most
    `max_videos`. Entries that are not a video are skipped. An extractor error raises
    `ChannelListingError` (with `blocked` set when YouTube refused us)."""
    try:
        info = extractor(url, max_videos=max_videos)
    except Exception as e:
        raise ChannelListingError(f"could not list {url}: {e}", blocked=is_block_error(e)) from e
    if not isinstance(info, dict):
        raise ChannelListingError(f"could not list {url}: the listing returned nothing")
    ids: list[str] = []
    for entry in _entries(info):
        vid = _entry_id(entry)
        if vid is not None and vid not in ids:
            ids.append(vid)
            if len(ids) >= max_videos:
                break
    return ids
