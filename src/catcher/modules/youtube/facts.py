from collections.abc import Callable, Sequence
from datetime import date
from typing import Any, cast

from pydantic import BaseModel


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


def fetch_facts(
    video_id: str, *, languages: Sequence[str] = ("en",), today: date | None = None
) -> YoutubeFacts:
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        info = _extract_info(url)
    except Exception as e:
        raise FactsUnavailable(f"yt-dlp failed for {video_id}: {e}") from e
    try:
        transcript = _fetch_transcript(video_id, languages)
    except Exception as e:
        raise FactsUnavailable(f"transcript fetch failed for {video_id}: {e}") from e
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
