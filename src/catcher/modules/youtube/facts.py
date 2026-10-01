import html
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any, cast

from pydantic import BaseModel

from catcher.modules.youtube.gate import is_block_error

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
    fetched_utc: str | None = (
        None  # when it was fetched, to the second: a video without captions is asked again after a day
    )

    def transcript_text(self) -> str | None:
        if not self.transcript:
            return None
        return "\n".join(f"[{fmt_ts(s.start_s)}] {s.text}" for s in self.transcript)


class FactsUnavailable(Exception):
    pass


class FactsDeferred(FactsUnavailable):
    """Not now: the gap between two YouTube calls has not passed, or the breaker is open. Try again later."""


FactsFetcher = Callable[[str], YoutubeFacts]


def _parse_json3_captions(data: dict[str, Any]) -> list[Segment]:
    """YouTube's own caption JSON format (yt-dlp's `json3` subtitle format)."""
    return [
        Segment(start_s=event.get("tStartMs", 0) / 1000, text=text)
        for event in data.get("events", [])
        if (text := "".join(seg.get("utf8", "") for seg in event.get("segs", []) if "utf8" in seg).strip())
    ]


_VTT_CUE_TIME = re.compile(r"^(\d+):(\d{2}):(\d{2})\.(\d{3}) -->")
_VTT_TAG = re.compile(r"<[^>]*>")


def _parse_vtt_captions(vtt: str) -> list[Segment]:
    """YouTube's auto-caption WebVTT. Each cue repeats the previous line and adds the new one
    (rolling captions, with inline word timestamps), so keep only the last text line of each cue
    and skip it when it just repeats the one before."""
    segments: list[Segment] = []
    for block in re.split(r"\n\n", vtt):
        lines = block.strip().splitlines()
        match = next((m for line in lines if (m := _VTT_CUE_TIME.match(line))), None)
        if match is None:
            continue
        hours, minutes, secs, millis = (int(g) for g in match.groups())
        text_lines = [html.unescape(_VTT_TAG.sub("", line)).strip() for line in lines if "-->" not in line]
        text = next((t for t in reversed(text_lines) if t), "")
        if text and (not segments or segments[-1].text != text):
            segments.append(Segment(start_s=hours * 3600 + minutes * 60 + secs + millis / 1000, text=text))
    return segments


def _captions_from(
    ydl: Any, info: dict[str, Any], languages: Sequence[str], delay_s: float, sleep: Callable[[float], None]
) -> list[Segment] | None:
    """The transcript, from the caption track yt-dlp already found: one more request."""
    tracks = info.get("requested_subtitles") or {}
    track = next((tracks[lang] for lang in languages if lang in tracks), None)
    if track is None:
        return None
    sleep(delay_s)  # the caption file is a request too: pace it like the others
    try:
        data = json.loads(ydl.urlopen(track["url"]).read())
    except Exception as e:
        if is_block_error(e):
            raise  # a block must reach the breaker, not hide as "no captions"
        log.warning("could not read the caption file: %s", e)
        return None
    return _parse_json3_captions(data) or None


def _extract(
    video_id: str,
    *,
    languages: Sequence[str],
    request_delay_s: float,
    skip_manifests: bool,
    sleep: Callable[[float], None],
) -> tuple[dict[str, Any], list[Segment] | None]:
    """ONE yt-dlp extraction for everything: the info and the caption track (the watch page, the player
    data, and the caption file). Every request is paced, and yt-dlp's own retries are kept to one, because
    retrying while YouTube is saying no only makes the block longer."""
    import yt_dlp

    params: dict[str, Any] = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": list(languages),
        "subtitlesformat": "json3",
        "sleep_interval_requests": request_delay_s,
        "retries": 1,
        "extractor_retries": 1,
    }
    if skip_manifests:  # we never download the video, so the list of formats is not needed
        params["extractor_args"] = {"youtube": {"skip": ["hls", "dash", "translated_subs"]}}
        params["ignore_no_formats_error"] = True
    with yt_dlp.YoutubeDL(cast(Any, params)) as ydl:
        info = cast(
            dict[str, Any], ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        )
        transcript = _captions_from(ydl, info, languages, request_delay_s, sleep)
    return info, transcript


def fetch_facts(
    video_id: str,
    *,
    languages: Sequence[str] = ("en",),
    today: date | None = None,
    now: datetime | None = None,
    request_delay_s: float = 10.0,
    skip_manifests: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> YoutubeFacts:
    """Fetch the facts of one video from YouTube: about 3 paced requests (the watch page, the player data
    and the caption file). The caller decides whether it may (the gap, the breaker, the saved facts)."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        info, transcript = _extract(
            video_id,
            languages=languages,
            request_delay_s=request_delay_s,
            skip_manifests=skip_manifests,
            sleep=sleep,
        )
    except Exception as e:
        raise FactsUnavailable(f"yt-dlp failed for {video_id}: {e}") from e
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
        fetched_utc=(now or datetime.now(UTC)).isoformat(timespec="seconds"),
    )
