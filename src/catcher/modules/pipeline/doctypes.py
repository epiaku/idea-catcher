import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from catcher.modules.youtube.urls import YOUTUBE_HOSTS, find_youtube_url, host_of, video_id

DOCS_ROOT = "hugo/content/en/docs/idea-bucket"
# The epiaku-docs folders under DOCS_ROOT a page can go to. A document's `destination` field names one.
DESTINATIONS = ("notes", "youtube", "web-clips")
CHAT_HOSTS = frozenset({"gemini.google.com", "claude.ai"})


def is_destination(value: object) -> bool:
    """Exactly one of the three names: a value from a file must never become a path unchecked."""
    return isinstance(value, str) and value in DESTINATIONS


def destination_dir(destination: str) -> str:
    """The folder of a destination in the epiaku-docs repo. Raises ValueError for any other value."""
    if not is_destination(destination):
        raise ValueError(f"unknown destination {destination!r}: expected one of {', '.join(DESTINATIONS)}")
    return f"{DOCS_ROOT}/{destination}"


@dataclass(frozen=True)
class DocType:
    name: str
    task: str
    schema_name: str
    template: str
    destination: str  # the default destination; a valid `destination` in the capture wins
    llm_profile: str


NOTE = DocType("note", "note", "NoteSummary", "note.md.j2", "notes", "notes")
AI_CHAT = DocType("ai-chat", "ai-chat", "ChatSummary", "ai-chat.md.j2", "web-clips", "clippings")
WEB_CLIP = DocType("web-clip", "web-clip", "WebClipSummary", "web-clip.md.j2", "web-clips", "clippings")
YOUTUBE = DocType("youtube", "youtube", "YoutubeSummary", "youtube.md.j2", "youtube", "youtube")
YOUTUBE_GEMINI = DocType(
    "youtube-gemini", "youtube-gemini", "YoutubeSummary", "youtube.md.j2", "youtube", "youtube"
)
DOC_TYPES: dict[str, DocType] = {t.name: t for t in (NOTE, AI_CHAT, WEB_CLIP, YOUTUBE, YOUTUBE_GEMINI)}

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")
_TURN_END = {"**Gemini**", "**Claude**", "---"}


def first_user_turn(body: str) -> str:
    lines = body.split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "**You**")
    except StopIteration:
        return body[:2000]
    turn: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() in _TURN_END:
            break
        turn.append(line)
    return "\n".join(turn)


def gemini_video_id(body: str) -> str | None:
    url = find_youtube_url(first_user_turn(body))
    return video_id(url) if url else None


def detect(fm: dict[str, Any], body: str) -> DocType:
    for key in ("type", "class"):
        value = str(fm.get(key) or "").strip()
        if value in DOC_TYPES:
            return DOC_TYPES[value]
    source = str(fm.get("source") or "")
    host = host_of(source) if source else ""
    if host in YOUTUBE_HOSTS and video_id(source):
        return YOUTUBE
    if host in CHAT_HOSTS:
        if host == "gemini.google.com" and gemini_video_id(body):
            return YOUTUBE_GEMINI
        return AI_CHAT
    if source.startswith(("http://", "https://")) and host:
        return WEB_CLIP  # any other page clipped with the Web Clipper
    return NOTE


def safe_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = _UNSAFE_ID.sub("-", raw).strip("-")
    return cleaned or None


def _last_path_segment(url: str) -> str | None:
    parts = [p for p in urlparse(url.strip()).path.split("/") if p]
    return parts[-1] if parts else None


_TRACKING_PARAM = re.compile(r"^(utm_.*|gclid|fbclid|igshid|mc_cid|mc_eid)$", re.IGNORECASE)


def clean_url(url: str) -> str:
    """The link without tracking parameters and without a fragment."""
    parsed = urlparse(url.strip())
    query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not _TRACKING_PARAM.match(k)
    ]
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


def _web_clip_id(source: str) -> str | None:
    parsed = urlparse(clean_url(source))
    if not parsed.netloc:
        return None
    host = parsed.netloc.lower().removeprefix("www.")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    normalized = f"{parsed.scheme.lower()}://{host}{parsed.path.rstrip('/')}" + (f"?{query}" if query else "")
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def derive_id(doctype: DocType, fm: dict[str, Any], body: str) -> str | None:
    if fm.get("id") not in (None, ""):
        return safe_id(str(fm["id"]).strip())
    source = str(fm.get("source") or "")
    if doctype is YOUTUBE:
        return safe_id(video_id(source))
    if doctype is YOUTUBE_GEMINI:
        vid = gemini_video_id(body)
        return f"{vid}-gemini" if vid else None
    if doctype is AI_CHAT:
        return safe_id(_last_path_segment(source))
    if doctype is WEB_CLIP:
        return _web_clip_id(source)  # the same page clipped again gets the same id
    return None


def canonical_source(doctype: DocType, fm: dict[str, Any]) -> str | None:
    source = str(fm.get("source") or "").strip()
    if not source:
        return None
    if doctype is YOUTUBE:
        vid = video_id(source)
        return f"https://www.youtube.com/watch?v={vid}" if vid else None
    if doctype in (AI_CHAT, YOUTUBE_GEMINI):
        parsed = urlparse(source)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.netloc else None
    if doctype is WEB_CLIP:
        return clean_url(source)
    return source
