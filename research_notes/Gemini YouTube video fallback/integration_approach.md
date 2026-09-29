# Gemini API YouTube Video Understanding as a Fallback Data Source (google-genai SDK, Sept/Oct 2026)

## What is the exact, current, correct way to call the `google-genai` Python SDK to pass a YouTube URL and a text prompt, and get back a text response?

### Takeaway
The current, correct, and most portable pattern uses `client.models.generate_content()` from the `google-genai` package (imported as `from google import genai`), passing a `types.Content` with a `types.Part(file_data=types.FileData(file_uri=<youtube_url>))` plus a text `types.Part`, and reading `response.text`. No `mime_type` needs to be (or should be) set for YouTube URLs specifically — this differs from the "uploaded file" `Part.from_uri(file_uri=..., mime_type=...)` pattern, which is for the File API, not raw YouTube links. Google has also begun rolling out a newer, higher-level "Interactions API" (`client.interactions.create(...)`) that accepts video input more declaratively, but `generate_content` remains the documented, stable, and widely-used entry point as of this research.

### Cited Findings
- Official "Video understanding (Legacy)" docs give this exact Python example for YouTube input:
```python
from google import genai
from google.genai import types

client = genai.Client()
response = client.models.generate_content(
    model="gemini-3.8-flash",
    contents=types.Content(
        parts=[
            types.Part(file_data=types.FileData(file_uri="https://www.youtube.com/watch?v=9hE5-98ZeCg")),
            types.Part(text="Please summarize the video in 3 sentences."),
        ]
    ),
)
print(response.text)
```
— [Video understanding | Gemini Generate Content API (Legacy)](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding)
- The same page notes the docs do **not** specify a `mime_type` parameter for YouTube URLs in the Python example (unlike some other-language examples, e.g. Go, which pass `"video/mp4"`); `Part.from_uri(file_uri=..., mime_type=...)` is shown elsewhere in the docs targeting **uploaded files via the File API**, not YouTube links directly. — [Video understanding | Gemini Generate Content API (Legacy)](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding)
- A simpler dict-based content shape also works and is shown in a community example (equivalent to the typed version above):
```python
from google import genai

client = genai.Client(api_key="YOUR_API_KEY")
youtube_url = "https://www.youtube.com/watch?v=tAP1eZYEuKA"

response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents=[
        {
            "parts": [
                {"text": "Can you summarize this video?"},
                {"file_data": {"file_uri": youtube_url}},
            ]
        }
    ],
)
print(response.text)
```
— [Gemini by Example: YouTube video summarization](https://geminibyexample.com/015-youtube-video-summarization/)
- A community/Google-moderator-confirmed fix for a long-video 500 error specifically flagged that using `"file_data"` (snake_case) rather than `"fileData"`/explicit `"mimeType": "video/*"` produced the working request shape — reinforcing that YouTube URL parts should be passed via `file_data`/`FileData` without a mime type override. — [Google AI Developers Forum: Understanding Long YouTube Videos with Gemini](https://discuss.ai.google.dev/t/understanding-long-youtube-videos-with-gemini/83207)
- A newer, separate "Interactions API" documentation page exists (`ai.google.dev/gemini-api/docs/interactions/video-understanding`) showing a higher-level call shape:
```python
from google import genai

client = genai.Client()
interaction = client.interactions.create(
    model="gemini-3.8-flash",
    input=[
        {"type": "text", "text": "Please summarize the video in 3 sentences."},
        {"type": "video", "uri": "https://www.youtube.com/watch?v=9hE5-98ZeCg"},
    ],
)
print(interaction.output_text)
```
This API supports "agentic" video navigation, where the model dynamically loads only the transcript/video segments it needs via `processing_call`/`processing_result` steps. — [Video understanding – Interactions API](https://ai.google.dev/gemini-api/docs/interactions/video-understanding)
- The `generate-content/video-understanding` page is explicitly titled "(Legacy)" in its page title, and the newer Interactions API is referenced in doc navigation, but as of this research there is **no prominent deprecation notice** telling developers to migrate away from `generate_content` for video use cases. — [Video understanding | Gemini Generate Content API (Legacy)](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding)

### Inferences
- For a "fallback step" use case (single call in, single text out, minimal new surface area), `client.models.generate_content()` is the safer choice: it's synchronous, well-documented, matches the shape most existing tutorials/StackOverflow answers use, and doesn't require adopting the newer agentic Interactions API's different response object (`interaction.output_text` vs `response.text`) and its own step/polling semantics.
- The Interactions API's agentic transcript-loading capability is conceptually attractive for "get a transcript-like output," but given it's clearly newer/less battle-tested (per the docs' own layered structure — "Legacy" vs current), it's a reasonable **future enhancement** rather than the first implementation for a fallback.

### Gaps
- I could not confirm from primary sources whether `client.interactions.create` is fully generally available (GA) vs. still preview/beta as of Sept/Oct 2026, or its exact SLA/stability compared to `generate_content`. Treat it as newer and less proven for production fallback use until verified directly against the live docs at implementation time.
- I did not find an official statement explicitly deprecating `generate_content` for video; the "(Legacy)" label is a naming signal, not a documented deprecation timeline.

---

## What does authentication/setup look like (API key env var, client construction)?

### Takeaway
Authentication is via an API key, most commonly read automatically from the `GEMINI_API_KEY` (or, per some legacy docs, `GOOGLE_API_KEY`) environment variable when you construct `genai.Client()` with no arguments; the key can also be passed explicitly as `genai.Client(api_key="...")`.

### Cited Findings
- The canonical minimal setup shown across current docs and examples is:
```python
from google import genai

client = genai.Client()
```
with the API key picked up implicitly from the environment. — [Video understanding | Gemini Generate Content API (Legacy)](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding); [Video understanding – Interactions API](https://ai.google.dev/gemini-api/docs/interactions/video-understanding)
- Explicit key passing is also documented/used in examples: `client = genai.Client(api_key="YOUR_API_KEY")`. — [Gemini by Example: YouTube video summarization](https://geminibyexample.com/015-youtube-video-summarization/)
- Package installation is `pip install google-genai` (this is the **current** SDK; the older, now-deprecated package is `google-generativeai`, whose GitHub repo has been renamed/relocated to `google-gemini/deprecated-generative-ai-python`). — [Gemini by Example: YouTube video summarization](https://geminibyexample.com/015-youtube-video-summarization/); [google-gemini/deprecated-generative-ai-python (GitHub)](https://github.com/google-gemini/deprecated-generative-ai-python/issues/574)
- A real-world authentication failure thread confirms `ClientError: 401 UNAUTHENTICATED` is the error surfaced by `google-genai` when the API key is missing/invalid. — [Google AI Developers Forum: Python genai.Client authentication failure](https://discuss.ai.google.dev/t/python-genai-client-authentication-failure/78979)

### Inferences
- Given the existing `idea-catcher` project already has "API-key LLM profiles" (per recent commit history: `feat: inbox-only flow with calculated file names, API-key LLM profiles and logging`), the fallback implementation should read the Gemini API key from whatever config/env convention the project already established for other API-key LLM profiles, rather than inventing a new env var name — this is a project-integration decision, not something the public docs can settle.

### Gaps
- I did not verify the precise current default env var name (`GEMINI_API_KEY` vs `GOOGLE_API_KEY`) with 100% certainty from a single authoritative current-dated source in this pass; different doc generations have used both. The implementation should explicitly pass `api_key=` sourced from the project's own config rather than relying on ambient env var auto-detection, to avoid ambiguity.

---

## What error types/exceptions does the SDK raise on failure (quota, invalid video, too long, region blocked, etc.) that a fallback implementation would need to catch?

### Takeaway
`google-genai` raises a small, structured exception hierarchy rooted at `APIError`, with `ClientError` for HTTP 4xx (e.g. 401 auth, 429 quota/rate-limit) and `ServerError` for HTTP 5xx (e.g. 500 on problematic/long videos); additionally, "soft failures" that don't raise at all — blocked/empty responses — must be detected by inspecting `response.prompt_feedback.block_reason` and `candidate.finish_reason` rather than relying on an exception.

### Cited Findings
- From the SDK's `errors.py` source: `APIError(Exception)` is the root; `ClientError(APIError)` and `ServerError(APIError)` are subclasses. The `raise_error()`/`raise_error_async()` logic maps HTTP status 400–499 → `ClientError`, 500–599 → `ServerError`, other codes → generic `APIError`. Each exposes `code`, `message`, and `status` attributes parsed from the response JSON, and `str(err)` renders as `"{code} {status}. {details}"`. — [python-genai errors.py](https://github.com/googleapis/python-genai/blob/main/google/genai/errors.py)
- Other non-API-error exceptions exist for function-calling misuse only (not relevant to plain video content generation): `UnknownFunctionCallArgumentError`, `UnsupportedFunctionError`, `FunctionInvocationError`, `UnknownApiResponseError` (all subclass `ValueError`). — [python-genai errors.py](https://github.com/googleapis/python-genai/blob/main/google/genai/errors.py)
- Real-world observed errors from GitHub issues/forums:
  - `ClientError: 429 RESOURCE_EXHAUSTED` — quota/rate limit exceeded, observed even on first use after an idle period. — [python-genai Issue #17](https://github.com/googleapis/python-genai/issues/17)
  - `ClientError: 401 UNAUTHENTICATED` — bad/missing API key. — [Google AI Developers Forum: authentication failure](https://discuss.ai.google.dev/t/python-genai-client-authentication-failure/78979)
  - `ServerError: 500` — observed both as a generic "Failed to convert server response to JSON" case (`client.files.list()`) — [python-genai Issue #815](https://github.com/googleapis/python-genai/issues/815) — and, more relevant here, as an intermittent failure when processing **hour-long YouTube videos**, with no output returned. — [Google AI Developers Forum: Understanding Long YouTube Videos](https://discuss.ai.google.dev/t/understanding-long-youtube-videos-with-gemini/83207)
  - **Region/billing gating**: the Gemini API free tier is not available in all regions, and paid/billing-enabled access is required in some cases — this can surface as an access/permission-style `ClientError` rather than a video-specific error. — search synthesis citing [Google AI Developers Forum threads on Gemini/video access](https://discuss.ai.google.dev/t/understanding-long-youtube-videos-with-gemini/83207)
  - **Silent/soft failures without an exception**: uploading a downloaded YouTube video via the File API (as opposed to passing the URL directly) can return a response with only `prompt_feedback` set and `BlockedReason.OTHER`, no exception and no usable candidate text — whereas passing the YouTube URL directly to `generate_content` succeeded for the same content. — [python-genai Issue #864: Video Understanding not working for File-API-uploaded video](https://github.com/googleapis/python-genai/issues/864)
  - **First-request truncation bug (no exception, wrong data)**: the first, uncached call against a given YouTube video can silently return a transcript/summary covering only 0.5%–27% of the actual video, with `prompt_token_count` identical between the truncated and full runs — i.e., no error signal at all, just wrong/incomplete output; a second (cached) call on the same video is reliable. This is a genuine SDK/API reliability gap as of the report date (issue open, unresolved). — [python-genai Issue #1898](https://github.com/googleapis/python-genai/issues/1898)
  - **Recent videos may be inaccessible**: with `gemini-3-flash-preview`, YouTube videos less than about a month old were reported as failing to load correctly (thumbnail shows, but title is wrong or the request times out) — a plausible proxy for "video too new / not yet indexed," separate from classic private/unlisted/region-blocked cases. — [Google AI Developers Forum: Recent YouTube Videos Inaccessible](https://discuss.ai.google.dev/t/recent-youtube-videos-inaccessible-via-gemini-3-flash-preview-api/114076)
- General blocked-prompt handling pattern (still applicable to `google-genai`, consistent with legacy `google-generativeai` semantics): check `response.prompt_feedback.block_reason` — if set, the whole prompt was blocked and no candidates are returned; also check each `candidate.finish_reason` and `candidate.safety_ratings` for partial/safety-truncated output. — [The Neural Base: When responses are blocked](https://theneuralbase.com/gemini-api/learn/beginner/when-responses-are-blocked/); [Gemini safety settings docs](https://ai.google.dev/gemini-api/docs/safety-settings)

### Inferences
- A robust fallback function must **not** treat "no exception raised" as "success." It needs to defensively check, in order: (1) exception raised (`ClientError`/`ServerError`/`APIError`) → clear failure; (2) `response.prompt_feedback.block_reason` set → treat as failure (video blocked/private/policy); (3) no candidates, or `candidate.finish_reason` indicating truncation/safety/max-tokens → treat as partial/unreliable and either retry or fail; (4) `response.text` empty or suspiciously short relative to expected video length → treat as likely truncation (per Issue #1898) and consider one retry, since the bug is observed to resolve itself on a second (cached) call.
- Given Issue #1898's caching behavior (first call flaky, second call reliable), a pragmatic fallback implementation should build in **one automatic retry** for suspiciously short/incomplete results before giving up, since the retry is cheap and the docs/issue suggest it reliably fixes the truncation.
- "Region blocked" and "video too long" don't appear to have dedicated, distinctly-named exception classes — they surface as generic `ClientError`/`ServerError` with varying HTTP codes/messages, so the fallback's error handling should be written to catch the exception hierarchy broadly (`APIError` and subclasses) and log `code`/`status`/`message` for diagnosis rather than trying to pattern-match specific known error strings.

### Gaps
- No official, authoritative list of "these are all the ways a YouTube-URL video request can fail" was found; the picture above is assembled from scattered GitHub issues and forum threads, which are real but not a comprehensive/guaranteed catalog. A production implementation should log full exception details and unexpected response shapes to build its own catalog over time.
- I found no confirmation of a dedicated exception type or explicit error code for "region blocked" specifically — this may simply surface as an empty/blocked response (via `block_reason`) or a `ClientError` with a permission-denied-style code rather than a video-specific error.

---

## Is there a way to request a literal transcript/caption-like output (timestamped text) versus just a free-form summary? What prompt engineering approaches produce a transcript-like, quotable output?

### Takeaway
There is no dedicated "give me raw captions" API parameter for YouTube URLs in `generate_content`; transcript-like output is obtained purely through prompt engineering, asking the model to produce a timestamped transcript using the documented `MM:SS` timestamp convention the model itself understands and can reference.

### Cited Findings
- The docs establish that Gemini samples video at ~1 frame per second and processes the audio track for transcription-like understanding, and that when a prompt itself references specific moments, it should use `MM:SS` format (e.g., `01:15` for 1 minute 15 seconds); this format is the one the model is documented to understand for timestamp-based reasoning about the video. — [Video understanding | Gemini API](https://ai.google.dev/gemini-api/docs/video-understanding) (search-tool synthesis); [Video understanding – Interactions API](https://ai.google.dev/gemini-api/docs/interactions/video-understanding)
- Example of timestamp-anchored prompting shown in docs: `"What are the examples given at 00:05 and 00:10 supposed to show us?"` — demonstrating the model can be asked to reason about/quote specific timestamped segments, which is the same mechanism usable in reverse (ask the model to *emit* timestamps alongside text). — [Video understanding | Gemini Generate Content API (Legacy)](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding)
- The newer Interactions API explicitly frames itself around agentic access to the video's timeline and "loads transcripts on demand," implying that under the hood the model/service does have transcript-like internal representations it navigates, even though this isn't exposed as a raw downloadable caption file via `generate_content`. — [Video understanding – Interactions API](https://ai.google.dev/gemini-api/docs/interactions/video-understanding)

### Inferences
- To get a transcript-like, quotable, fallback-suitable output from `generate_content`, the prompt itself should explicitly instruct the model to produce a structured, timestamped transcript, e.g.:
  > "Produce a verbatim-as-possible transcript of the spoken audio in this video. Format each line as `MM:SS — <text>`. Do not summarize or paraphrase; if a section is unclear, write `[inaudible]`. Cover the entire video from start to end."
  This leverages the model's documented `MM:SS` timestamp understanding and its instruction-following ability, rather than any dedicated "captions" API flag (none exists in the current `google-genai` SDK for YouTube URLs).
- Because Issue #1898 shows the model can silently truncate on first pass, a transcript-style prompt should also ask the model to state the video's total apparent duration or end with an explicit "END OF TRANSCRIPT" marker, so the calling code has a cheap heuristic to detect truncated/incomplete output (e.g., missing end marker → retry).
- For a "fallback data source" whose job is to approximate `youtube_transcript_api`'s output shape (list of timed segments), the calling code will likely need to **parse** Gemini's free-text `MM:SS — text` lines into structured segments itself (simple regex per line), since the API returns plain text, not a structured caption object, via `generate_content`.

### Gaps
- I found no official Google documentation offering a "transcript mode" flag or `response_schema` guidance specifically tuned for verbatim video transcription (as opposed to general structured-output/JSON-mode features that exist for other Gemini use cases). If exact verbatim fidelity matters, note explicitly in the report that this is prompt-engineered best-effort, not a guaranteed verbatim ASR transcript, and accuracy should be validated empirically against known videos before relying on it.
- I did not find third-party blog posts/tutorials (2025–2026) specifically benchmarking prompt phrasings for maximizing transcript fidelity; the guidance above is inferred from documented model capabilities (frame sampling rate, `MM:SS` referencing) rather than an explicit "best prompt" source.

---

## What would integration code look like as a fallback tried only after `youtube_transcript_api` and `yt-dlp` subtitle download have both failed?

### Takeaway
A third-tier fallback function should mirror the interface of the two primary methods (accept a video ID/URL, return the same transcript-like data shape or `None`/raise on definitive failure), wrap the Gemini call in the exception/response-inspection checks established above, and include one bounded retry for the known first-call truncation issue before giving up.

### Cited Findings
This section is a code sketch synthesized directly from the primary-source API shapes and error-handling facts cited above (each individual API element is sourced there); no additional new external claims are introduced here beyond what's already cited.

```python
"""
gemini_youtube_fallback.py

Third-tier fallback transcript source: only invoked after
`youtube_transcript_api` and `yt-dlp` subtitle download have both failed.

Requires: pip install google-genai
API surface confirmed against:
- https://ai.google.dev/gemini-api/docs/generate-content/video-understanding
- https://github.com/googleapis/python-genai (errors.py)
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from google import genai
from google.genai import types
from google.genai import errors as genai_errors

logger = logging.getLogger(__name__)


# Match the interface used by the primary methods, e.g.:
#   fetch_transcript_youtube_transcript_api(video_id) -> list[TranscriptSegment] | None
#   fetch_transcript_ytdlp(video_id) -> list[TranscriptSegment] | None
@dataclass
class TranscriptSegment:
    start_seconds: float
    text: str


_TRANSCRIPT_PROMPT = (
    "Produce a transcript of the spoken audio in this video. "
    "Format EVERY line as `MM:SS — <text>` with one line per utterance or "
    "sentence. Do not summarize or paraphrase — transcribe as closely to "
    "verbatim as you can. If a section is unclear, write `[inaudible]`. "
    "Cover the ENTIRE video from 00:00 to the end. "
    "Finish your output with a final line that says exactly: END OF TRANSCRIPT"
)

_TIMESTAMP_LINE_RE = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s*[—\-–]\s*(.+)$")


def _parse_mm_ss_to_seconds(hours: str | None, minutes: str, seconds: str) -> float:
    h = int(hours) if hours else 0
    m = int(minutes)
    s = int(seconds)
    return float(h * 3600 + m * 60 + s)


def _parse_transcript_text(text: str) -> tuple[list[TranscriptSegment], bool]:
    """Parse Gemini's `MM:SS — text` lines into segments.

    Returns (segments, looks_complete). `looks_complete` is a cheap
    truncation heuristic based on the presence of the end marker
    (see the first-call truncation issue noted in the research notes:
    googleapis/python-genai#1898).
    """
    segments: list[TranscriptSegment] = []
    looks_complete = "END OF TRANSCRIPT" in text
    for line in text.splitlines():
        m = _TIMESTAMP_LINE_RE.match(line)
        if not m:
            continue
        hours, minutes, seconds, content = m.groups()
        segments.append(
            TranscriptSegment(
                start_seconds=_parse_mm_ss_to_seconds(hours, minutes, seconds),
                text=content.strip(),
            )
        )
    return segments, looks_complete


class GeminiYouTubeFallbackError(Exception):
    """Raised when the Gemini fallback itself cannot produce a transcript."""


def fetch_transcript_gemini_fallback(
    video_url: str,
    *,
    api_key: str,
    model: str = "gemini-2.5-flash",
    max_attempts: int = 2,
    request_timeout_s: float = 120.0,
) -> list[TranscriptSegment]:
    """Third-tier fallback: ask Gemini's video understanding to produce a
    transcript-like text for a YouTube URL.

    Only call this after `youtube_transcript_api` and yt-dlp subtitle
    download have both failed. Raises GeminiYouTubeFallbackError on
    definitive failure (caller should treat that the same as the other
    two methods returning None/raising).
    """
    client = genai.Client(api_key=api_key)

    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=types.Content(
                    parts=[
                        types.Part(file_data=types.FileData(file_uri=video_url)),
                        types.Part(text=_TRANSCRIPT_PROMPT),
                    ]
                ),
                # Optional: bound generation time/cost for a fallback path.
                config=types.GenerateContentConfig(
                    max_output_tokens=8192,
                ),
            )
        except genai_errors.ClientError as e:
            # 4xx: bad request, 401 unauthenticated, 429 quota exceeded,
            # permission/region-gated access, etc. Not worth retrying
            # blindly except for 429, which callers may want to back off on.
            logger.warning(
                "Gemini fallback ClientError (code=%s status=%s): %s",
                getattr(e, "code", None),
                getattr(e, "status", None),
                e,
            )
            last_error = e
            if getattr(e, "code", None) == 429 and attempt < max_attempts:
                time.sleep(2**attempt)
                continue
            break
        except genai_errors.ServerError as e:
            # 5xx: observed intermittently on long/problematic videos.
            logger.warning(
                "Gemini fallback ServerError (code=%s status=%s): %s",
                getattr(e, "code", None),
                getattr(e, "status", None),
                e,
            )
            last_error = e
            if attempt < max_attempts:
                time.sleep(2**attempt)
                continue
            break
        except genai_errors.APIError as e:
            logger.warning("Gemini fallback APIError: %s", e)
            last_error = e
            break

        # No exception was raised — but that does not mean we got usable
        # text. Check for a blocked prompt first.
        feedback = getattr(response, "prompt_feedback", None)
        block_reason = getattr(feedback, "block_reason", None)
        if block_reason:
            last_error = GeminiYouTubeFallbackError(
                f"Gemini blocked the request: block_reason={block_reason} "
                "(commonly: private/unlisted video, policy-restricted "
                "content, or region-gated access)"
            )
            logger.warning(str(last_error))
            break

        text = getattr(response, "text", None)
        if not text:
            last_error = GeminiYouTubeFallbackError(
                "Gemini returned no text (possible content/finish_reason "
                f"issue: {getattr(response, 'candidates', None)})"
            )
            logger.warning(str(last_error))
            if attempt < max_attempts:
                continue
            break

        segments, looks_complete = _parse_transcript_text(text)

        if not segments:
            last_error = GeminiYouTubeFallbackError(
                "Gemini response contained no parseable MM:SS transcript lines; got free-form text instead."
            )
            logger.warning(str(last_error))
            if attempt < max_attempts:
                continue
            break

        if not looks_complete and attempt < max_attempts:
            # Known issue: first, uncached calls can silently truncate
            # (googleapis/python-genai#1898). A second call on the same
            # video tends to hit cache and return complete output.
            logger.info(
                "Gemini transcript looks possibly truncated (no end marker found); retrying (attempt %d/%d)",
                attempt + 1,
                max_attempts,
            )
            continue

        return segments

    raise GeminiYouTubeFallbackError(
        f"Gemini YouTube fallback failed after {max_attempts} attempt(s): {last_error}"
    ) from last_error


# Example orchestration showing the intended three-tier call order:
def get_transcript_with_fallbacks(video_id: str, video_url: str, *, gemini_api_key: str):
    transcript = fetch_transcript_youtube_transcript_api(video_id)  # tier 1 (external)
    if transcript:
        return transcript

    transcript = fetch_transcript_ytdlp(video_id)  # tier 2 (external)
    if transcript:
        return transcript

    try:
        return fetch_transcript_gemini_fallback(video_url, api_key=gemini_api_key)
    except GeminiYouTubeFallbackError:
        logger.exception("All three transcript sources failed for %s", video_id)
        return None
```

### Inferences
- The sketch deliberately keeps the Gemini call's *output contract* (`list[TranscriptSegment]`) aligned with what `youtube_transcript_api` typically returns (a sequence of timed text segments), so the caller-facing interface of tier 3 matches tiers 1–2 and existing downstream code doesn't need special-casing.
- Retries are capped and specifically targeted at (a) HTTP 429 (quota) with exponential backoff, and (b) the documented first-call truncation bug (heuristically detected via a required end marker) — both are backed by findings above rather than generic "retry on any failure," to avoid masking definitive failures (e.g., blocked/private video) with pointless retries.
- `max_output_tokens` and `model` are left as tunable parameters; a fallback path used sparingly can reasonably default to a fast/cheap model (e.g. `gemini-2.5-flash`/`gemini-2.0-flash`-class) rather than a top-tier reasoning model, since the task is transcription-style, not complex reasoning — this is an engineering judgment call, not something asserted by a cited source.

### Gaps
- No source gave a canonical/official reference implementation of a "tiered fallback with youtube_transcript_api + yt-dlp + Gemini" — this section's code is original synthesis built on the sourced API facts above, not a found third-party example, and should be treated as a starting sketch to adapt and test against real videos, not as a verbatim official recipe.
- Real-world verification (actually running this against a handful of test videos, including a long one and a private/blocked one) was outside the scope of this research pass and should be done before relying on the truncation heuristic and retry policy in production.
