#!/usr/bin/env python3
"""One manual trial: ask the Gemini API to watch ONE public YouTube video.

Not part of the service and never run by tests (it makes a real, billable-or-quota-limited API call).
It copies the call that the /watch skill makes (docs/idea-catcher-youtube-gemini-api-direct.md): the
Interactions API with a video part and `processing: agentic`. Standard library only.

    GEMINI_API_KEY=... uv run python scripts/gemini-watch-trial.py URL
    uv run python scripts/gemini-watch-trial.py URL --dry-run        # prints the request, calls nothing
    uv run python scripts/gemini-watch-trial.py URL --out tmp/trial/a.md --raw-out tmp/trial/a.json

The key comes from the environment, else from GEMINI_API_KEY in `.env` in the current directory. It is
sent only in a request header and never printed. The video is public-only: a private or unlisted video fails.
"""

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_MODEL = "gemini-3.7-flash"  # the /watch default; any model id works
DEFAULT_TIMEOUT_S = 600.0
YOUTUBE = re.compile(
    r"^https?://(?:(?:www\.|m\.|music\.)?youtube\.com/(?:watch\?\S*\bv=|shorts/|live/)|youtu\.be/)\S+$", re.I
)
DEFAULT_QUESTION = (
    "Give a thorough summary of this video: what is shown, any on-screen text or code, "
    "and what is said, in chronological order."
)
PROMPT_SUFFIX = (
    "\n\nCite an MM:SS (or H:MM:SS) timestamp for every claim about a specific moment. "
    "Separate what is seen from what is heard when it matters. If the video does not "
    "show or say something needed to answer, say so plainly instead of guessing."
)


def parse_clock(text: str) -> float:
    """SS, MM:SS or HH:MM:SS to seconds."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"bad time {text!r}: use SS, MM:SS or HH:MM:SS")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"bad time {text!r}")
    return seconds


def build_payload(url: str, question: str | None, model: str, start: str | None, end: str | None) -> dict:
    """Whole video: agentic processing. A time range only works in static mode (Google's rule)."""
    if start is None and end is None:
        processing: object = "agentic"
    else:
        config: dict[str, str] = {"type": "static"}
        if start is not None:
            config["start_offset"] = f"{math.floor(parse_clock(start))}s"
        if end is not None:
            config["end_offset"] = f"{math.ceil(parse_clock(end))}s"
        processing = config
    text = (question or "").strip() or DEFAULT_QUESTION
    return {
        "model": model,
        "input": [
            {"type": "video", "uri": url, "processing": processing},
            {"type": "text", "text": text + PROMPT_SUFFIX},
        ],
    }


def load_key() -> str | None:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    env_file = Path(".env")
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            name, _, value = line.strip().partition("=")
            if name.strip() == "GEMINI_API_KEY":
                return value.strip().strip("\"'") or None
    return None


def error_message(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
        if isinstance(data, list) and data:
            data = data[0]
        return str(data["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return text


def call(payload: dict, key: str, timeout: float) -> tuple[dict, dict[str, str]]:
    request = Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https endpoint)
            headers = {k.lower(): v for k, v in response.headers.items()}
            return json.loads(response.read()), headers
    except HTTPError as exc:
        detail = error_message(exc.read()).replace(key, "[redacted]")
        hint = {401: "check the key", 403: "check the key", 429: "rate limit or daily quota reached"}.get(
            exc.code,
            "the video may be private, unlisted, unsupported or too long"
            if exc.code < 500
            else "Google error",
        )
        raise SystemExit(f"HTTP {exc.code} ({hint}): {detail}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise SystemExit(f"network error: {type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None


def answer_text(data: dict) -> str:
    parts = [
        part["text"]
        for step in data.get("steps", [])
        if step.get("type") == "model_output"
        for part in step.get("content", [])
        if part.get("type") == "text" and part.get("text")
    ]
    return "\n".join(parts).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Ask Gemini to watch one public YouTube video (manual trial).")
    ap.add_argument("url", help="a public YouTube video URL (watch, shorts, live or youtu.be)")
    ap.add_argument("--question", help="what to ask (default: a thorough summary)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"model id (default {DEFAULT_MODEL})")
    ap.add_argument("--start", help="range start (SS, MM:SS, HH:MM:SS); switches to static mode")
    ap.add_argument("--end", help="range end; switches to static mode")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="seconds (default 600)")
    ap.add_argument("--out", type=Path, help="also save the answer as markdown here")
    ap.add_argument("--raw-out", type=Path, help="also save the full JSON response here")
    ap.add_argument("--dry-run", action="store_true", help="print the request body and stop: no key, no call")
    args = ap.parse_args()

    if not YOUTUBE.match(args.url):
        raise SystemExit(
            "not a YouTube video URL (channels and playlists are not accepted: one video per call)"
        )
    try:
        payload = build_payload(args.url, args.question, args.model, args.start, args.end)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

    if args.dry_run:
        print(
            json.dumps({"POST": ENDPOINT, "header": "x-goog-api-key: <not sent>", "body": payload}, indent=2)
        )
        return 0

    key = load_key()
    if not key:
        raise SystemExit(
            "no GEMINI_API_KEY: set it in the environment or in .env (create one at https://aistudio.google.com/apikey)"
        )

    print(f"[trial] asking {args.model} about {args.url} ...", file=sys.stderr)
    started = time.monotonic()
    data, headers = call(payload, key, args.timeout)
    elapsed = time.monotonic() - started

    text = answer_text(data)
    if not text:
        raise SystemExit(
            f"no answer text (status {data.get('status')}); use --raw-out to inspect the response"
        )
    usage = data.get("usage") or {}
    limit_headers = ", ".join(f"{k}={v}" for k, v in headers.items() if "ratelimit" in k or "quota" in k)
    mode = "agentic" if args.start is None and args.end is None else "static clip"
    report = "\n".join(
        [
            "# Gemini watch trial",
            "",
            f"- **Video:** {args.url}",
            f"- **Model:** {args.model} ({mode})",
            f"- **Seconds:** {elapsed:.1f}",
            f"- **Tokens:** {usage.get('total_tokens')} total (usage as returned: {json.dumps(usage)})",
            f"- **Rate-limit headers:** {limit_headers or 'none'}",
            "",
            "## Answer (from Gemini)",
            "",
            text,
            "",
        ]
    )
    print(report)
    for path, content in ((args.out, report), (args.raw_out, json.dumps(data, indent=2))):
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            print(f"[trial] saved {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
