import re
from urllib.parse import parse_qs, urlparse

YOUTUBE_HOSTS = frozenset({"youtube.com", "youtu.be", "music.youtube.com", "youtube-nocookie.com"})
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_URL = re.compile(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/[^\s)\]>\"'<*]+")


def host_of(url: str) -> str:
    netloc = urlparse(url.strip()).netloc.lower().split("@")[-1].split(":")[0]
    for prefix in ("www.", "m."):
        netloc = netloc.removeprefix(prefix)
    return netloc


def video_id(url: str) -> str | None:
    url = url.replace("\\", "").strip()
    parsed = urlparse(url)
    host = host_of(url)
    parts = [p for p in parsed.path.split("/") if p]
    candidate: str | None = None
    if host == "youtu.be":
        candidate = parts[0] if parts else None
    elif host in YOUTUBE_HOSTS:
        if parts[:1] == ["watch"]:
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        elif len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            candidate = parts[1]
    return candidate if candidate and _VIDEO_ID.match(candidate) else None


def find_youtube_url(text: str) -> str | None:
    for match in _YOUTUBE_URL.finditer(text.replace("\\", "")):
        if video_id(match.group(0)):
            return match.group(0)
    return None
