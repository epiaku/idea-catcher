import logging
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any, cast

from pydantic import BaseModel

log = logging.getLogger("catcher.youtube")


class Chapter(BaseModel):
    start_s: float
    title: str


class Segment(BaseModel):
    start_s: float
    text: str


def fmt_ts(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class YoutubeFacts(BaseModel):
    video_id: str
    url: str
    title: str | None = None
    channel: str | None = None
    subscribers: int | None = None
    views: int | None = None
    likes: int | None = None
    upload_date: str | None = None
    duration_s: int | None = None
    description: str | None = None
    chapters: list[Chapter] = []
    transcript: list[Segment] | None = None
    fetched_at: str

    def transcript_text(self) -> str | None:
        if not self.transcript:
            return None
        return "\n".join(f"[{fmt_ts(s.start_s)}] {s.text}" for s in self.transcript)


class FactsUnavailable(Exception):
    pass


FactsFetcher = Callable[[str], YoutubeFacts]


def _extract_info(url: str) -> dict[str, Any]:
    import yt_dlp

    params = cast(Any, {"skip_download": True, "quiet": True, "no_warnings": True})
    with yt_dlp.YoutubeDL(params) as ydl:
        return cast(dict[str, Any], ydl.sanitize_info(ydl.extract_info(url, download=False)))


def _fetch_transcript(video_id: str, languages: Sequence[str]) -> list[Segment] | None:
    from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi

    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=list(languages))
    except (TranscriptsDisabled, NoTranscriptFound):
        return None
    return [Segment(start_s=snippet.start, text=snippet.text) for snippet in fetched]


def _parse_json3_captions(data: dict[str, Any]) -> list[Segment]:
    """YouTube's own caption JSON format (yt-dlp's `json3` subtitle format)."""
    return [
        Segment(start_s=event.get("tStartMs", 0) / 1000, text=text)
        for event in data.get("events", [])
        if (text := "".join(seg.get("utf8", "") for seg in event.get("segs", []) if "utf8" in seg).strip())
    ]


def _fetch_transcript_via_ytdlp(video_id: str, languages: Sequence[str]) -> list[Segment] | None:
    """A second, independent path to the same captions, for when `youtube_transcript_api` fails
    outright (for example a blocked request). yt-dlp hits a different YouTube endpoint, so a
    block on one does not always mean the other is blocked too.

    The caption file itself is fetched through yt-dlp's own HTTP client (`ydl.urlopen`), not a
    plain `urllib` request: YouTube's caption endpoint expects the browser-like headers yt-dlp
    already sends for its other requests, and a bare request without them gets rate-limited too.
    """
    import json

    import yt_dlp

    params = cast(
        Any,
        {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": list(languages),
            "subtitlesformat": "json3",
        },
    )
    with yt_dlp.YoutubeDL(params) as ydl:
        info = cast(
            dict[str, Any],
            ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False),
        )
        tracks = info.get("requested_subtitles") or {}
        track = next((tracks[lang] for lang in languages if lang in tracks), None)
        if track is None:
            return None
        data = json.loads(ydl.urlopen(track["url"]).read())
    return _parse_json3_captions(data) or None


def _best_effort_transcript(video_id: str, languages: Sequence[str]) -> list[Segment] | None:
    """Try both transcript sources; give up quietly rather than failing this whole fetch.

    A blocked or failed fetch is treated the same as "this video genuinely has no captions":
    `fetch_facts()` still returns the rest (title, counts, description) instead of raising, so
    the caller gets real facts even when the transcript is missing. What the caller then does
    without a transcript is its own call: `_process_youtube()` in `pipeline/process.py` defers
    the document rather than asking the LLM to summarize from a title and description alone.
    """
    try:
        return _fetch_transcript(video_id, languages)
    except Exception as e:
        log.warning("transcript fetch failed for %s, trying yt-dlp instead: %s", video_id, e)
    try:
        transcript = _fetch_transcript_via_ytdlp(video_id, languages)
    except Exception as e:
        log.warning("yt-dlp transcript fallback failed for %s too, continuing without one: %s", video_id, e)
        return None
    if transcript is None:
        log.warning("yt-dlp found no captions for %s either, continuing without a transcript", video_id)
    return transcript


def fetch_facts(
    video_id: str, *, languages: Sequence[str] = ("en",), today: date | None = None
) -> YoutubeFacts:
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        info = _extract_info(url)
    except Exception as e:
        raise FactsUnavailable(f"yt-dlp failed for {video_id}: {e}") from e
    transcript = _best_effort_transcript(video_id, languages)
    raw_date = str(info.get("upload_date") or "")
    return YoutubeFacts(
        video_id=video_id,
        url=url,
        title=info.get("title"),
        channel=info.get("channel") or info.get("uploader"),
        subscribers=info.get("channel_follower_count"),
        views=info.get("view_count"),
        likes=info.get("like_count"),
        upload_date=f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 else None,
        duration_s=int(info["duration"]) if info.get("duration") else None,
        description=info.get("description"),
        chapters=[
            Chapter(start_s=c.get("start_time") or 0, title=c.get("title") or "")
            for c in info.get("chapters") or []
        ],
        transcript=transcript,
        fetched_at=(today or date.today()).isoformat(),
    )
