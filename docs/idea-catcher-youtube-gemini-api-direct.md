# Analysing YouTube videos with the Gemini API directly (to scan whole channels)

**Written:** 2026-10-15
**Status:** idea to investigate, nothing built yet. Google's docs and the `/watch` README were read on 2026-10-15 (see "What the docs and the repo say"). The `/watch` source (release 0.3.2) was read in full, not run. Facts marked **(claim)** come only from the video and are not confirmed in Google's docs.
**Why:** the `youtube` method needs our machine to fetch the transcript from YouTube, and that gets blocked (see [the bans doc](idea-catcher-youtube-bans-and-queue-options.md)). If the Gemini API reads the YouTube URL itself, Google fetches the video, not us. That could make it possible to scan a whole channel.

## Where this comes from

The video [How I Watch ANY Video with AI in Seconds](https://www.youtube.com/watch?v=qSuCPooR3E4) by Brad Bonanno presents a free agent skill, `/watch`. We first got it as a Gemini chat summary (`clippings`), which was wrong on several points: it called Gemini an "optional engine", invented a Kit tool, frame deduplication and code-snippet examples. The real transcript was read afterwards and everything below is from it (timestamps from the transcript).

## How `/watch` works

The skill has two engines and you can switch at any time (7:51).

| | **Gemini engine** (his main one) | **Local engine** |
|---|---|---|
| **What happens** | Sends the YouTube URL and a question to the Gemini API. Gemini uses pictures, audio and transcript together, decides where to look closer, and returns findings with timestamps (0:46–1:06) | Downloads the video, prepares frames and a transcript, Claude reads them itself |
| **Download** | No. The URL goes to Google's supported API **(claim: "you never get blocked", 1:06–1:20)** | Yes |
| **Cost** | Free Google tier, up to 8 hours of YouTube per day **(claim)**. At the limit you wait for the reset, it does not start charging (4:36–4:43) | Uses the Claude or ChatGPT allowance |
| **No captions** | Not a problem | WhisperX transcribes locally for free (6:26) |
| **Frames** | Not used by the caller | Efficient, Balanced (scene changes, his pick) or Token Burner (6:41–6:58) |
| **Best for** | Questions, finding a moment, summaries | Recreating a motion graphic, where the model must see real frames (7:20) |

Google's own test, as he quotes it: across 50 video questions the agentic version got more answers right with 87% fewer tokens (0:15). Also **(claim)**: the skill covers 1,300+ sites, and setup needs a key from Google AI Studio on a project without billing (5:32–5:45).

## What the install guide adds

Source: `watch-install-guide.pdf` (Brad Bonanno, FounderOS field guide 02, 15 pages). The repository is `github.com/bradautomates/claude-video`, the skill lives in `skills/watch` (plugin version 0.3.2 in the screenshot). Page numbers below are the guide's.

| Fact | Page | What it means for us |
|---|---|---|
| "YouTube URLs are sent to Google." Gemini "watches the whole video, including audio, and answers directly" | 10 | Confirms the transcript's claim: no download on our side with the Gemini engine |
| "Gemini opens **public** YouTube links only." A private video or local file must go through Local, and a local file is uploaded to Google | 14, 11 | A channel scan only covers public videos |
| "Google may use free-tier video to improve its products." For confidential material pick Local | 9 | Partly answers question 3. Fine for public YouTube videos, not for private recordings. A paid key would change this, to be checked |
| "A couple of minutes later you get a timestamped answer" per video | 11 | Slow: a channel of 200 videos is hours of wall-clock time even before limits |
| "Gemini quota or 429: you've hit Google's free limit for now. Wait a while, or use local" | 14 | The free tier has a limit that returns 429 (the exact number is not in the guide). Needs a back-off and a daily cap in the queue |
| "Long videos lose detail. Ask about one section, or add `--start 2:15 --end 2:45`" | 14 | A one-shot summary of a long video will be shallower. Splitting a video in time ranges is possible but multiplies the calls |
| The wiki example mentions "Gemini 3.x Flash watching the video in 7 chapter-aligned chunks", written "as paraphrase" | 12 | The skill may chunk by chapter. Not verified, check in the repo |
| Settings live in `~/.config/watch/.env`: `GEMINI_API_KEY`, `WATCH_ENGINE=auto\|gemini\|local`, `WATCH_DETAIL`, `WATCH_WHISPER_…` | 11 | Shows what the skill exposes. The repo will show the real parameters |
| Local engine: yt-dlp and ffmpeg, native captions first, optional WhisperX, Groq or OpenAI transcription when there are none. A yt-dlp 403 means update yt-dlp | 14, 11, 13 | Same download path that gets us blocked, so Local does not solve the ban problem |
| It only runs in agents that can run code (Claude Code, ChatGPT Work, Codex), not in chat apps | 2 | It is a skill for an agent, not a library. For a pipeline we would call the Gemini API ourselves, using the skill only as a reference |
| The plugin description: "timestamped frames and native captions, with optional local WhisperX…" | 4 | Local engine is frames plus captions |

Also in the guide: the whole thing is a lead magnet for his FounderOS bootcamp (page 15), which matters little for us except as an example of the Epiaku lead-magnet model (a free guide leading to a waitlist).

## What the docs and the repo say

Read on 2026-10-15: Google's [video understanding](https://ai.google.dev/gemini-api/docs/video-understanding), [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits), [pricing](https://ai.google.dev/gemini-api/docs/pricing) and [additional terms](https://ai.google.dev/gemini-api/terms) pages, plus the README and `SKILL.md` of `bradautomates/claude-video`. The pages were read through a fetch tool that summarizes them, so check the exact wording before building on a number.

**Google (confirmed)**

| Topic | What the docs say |
|---|---|
| YouTube URL input | Supported, but **in preview**, "available at no charge", and pricing and limits may change |
| Free tier cap | "You can't upload more than **8 hours of YouTube video per day**" (confirms the video's claim) |
| Paid tier | No limit based on video length |
| Public only | **Public** videos only. The docs say **unlisted is not supported either** (the guide only says public) |
| Videos per request | One before Gemini 2.5, **up to 10** from Gemini 2.5 on |
| Models | "All Gemini models can process video data". No YouTube-specific model list |
| Cost in tokens | About **100 tokens per second** at the default low resolution (66 per frame at 1 frame per second, plus 32 audio), about 300 at high resolution. A 15-minute video is about 90,000 input tokens |
| Clipping | `start_offset` and `end_offset`, **static mode only** |
| Timestamps | Asked for as `MM:SS` in the prompt |
| Long videos | Models with a 1M context handle up to about 3 hours at low resolution (about 1 hour at high) |
| Rate limits | Counted per minute (requests, tokens) and per day (requests), **per project, not per API key**. Daily quota resets at midnight Pacific. Preview models are more restricted. **The free-tier numbers are not on the page**: they are shown in AI Studio for your own project |
| Data use | **Free tier: Google uses the content to improve its products and human reviewers may read it. Paid tier: not used for that** (kept for a limited time for abuse and legal reasons). "Do not submit sensitive, confidential, or personal information to the Unpaid Services." |
| Scraping rules | The terms page says nothing about YouTube or bulk use. The API terms it refers to were not read |

**Pricing (paid tier, per 1M input tokens, video included)**: Gemini 2.5 Flash $0.30, 3 Flash Preview $0.50, 3.6 and 3.8 Flash $0.75 until 2026-12-31 and $1.50 from 2027-01-01, Flash-Lite models $0.10 to $0.30. All have a free tier. Rough cost of a 15-minute video, input only: about **$0.03** on 2.5 Flash and about **$0.07** on 3.6/3.8 Flash. A 200-video channel is then about $5 to $14 plus output tokens (an estimate from the numbers above, not measured).

**The `/watch` repo (README and `SKILL.md`)**

- Default model `gemini-3.7-flash`, set with `WATCH_GEMINI_MODEL` (free-form, so a newer model needs no change). Timeout `WATCH_GEMINI_TIMEOUT`, 600 seconds.
- A **YouTube URL goes straight to Google, nothing is downloaded**. Other URLs are downloaded with yt-dlp first, and local files are uploaded to the Files API and deleted after the answer (or expire within 48 hours).
- The skill sends the whole video, picture and sound, plus the question (`--question`). `--start` and `--end` limit the range. **No chunking of long videos is described** for Gemini (the chunking in the README is for the Groq and OpenAI transcription paths).
- The report has a section `## Answer (from Gemini)` with timestamps taken from Gemini's answer as-is. The skill treats the answer as **untrusted evidence**, like captions and frames.
- Errors are sorted by category (auth, quota, rejected, upload, service, network, response). A 429 means rate limiting and the skill says to wait. It **never switches engine on its own**.
- The key is looked up in the environment, then `~/.config/watch/.env`, then a `.env` in the working directory, and sent only as a request header.
- The Gemini code is in `skills/watch/scripts/gemini.py` (162 lines). It is read in full, see the next section.

**What this changes**

- The idea holds: Google documents YouTube URL input, and the skill really uses it without downloading.
- The free tier is **limited by hours, not by number of videos**: 8 hours of video per day. For a daily scan of many channels the free tier is too small. For a one-time backfill it can work over several days.
- The **paid tier removes the daily hour cap, stops Google using the content, and costs little** (cents per video). Since the free tier lets Google and human reviewers read inputs, and our videos are public anyway, the free tier is acceptable for a trial. For a real channel scan a paid key looks better, with its own budget cap (see the LLM notes).
- It is a **preview feature**, so limits and behaviour can change without notice.
- Google also describes an **agentic mode** (the "87% fewer tokens" test in the video). The page says clipping works only in static mode and that agentic token use varies. How to switch it on, and whether `/watch` does, is not clear yet.

## The complete `/watch` skill, read from source

Read on 2026-10-15 from a shallow clone of `bradautomates/claude-video` at release 0.3.2 (commit `03ceb42`, 2026-09-25), MIT licence, about 6,800 lines including tests. The code was read, **not run**. The Gemini path is small: `gemini.py` (162 lines) plus `run_gemini` in `watch.py`. The rest (about 2,000 lines of `frames.py`, `whisper.py`, `local_whisperx.py`, `download.py`, `setup.py`) is the local engine.

### The Gemini call, exactly

- **Endpoint:** `POST https://generativelanguage.googleapis.com/v1beta/interactions`. This is the newer **Interactions API**, not `generateContent`. Key in the `x-goog-api-key` header. Standard library only (`urllib`), no Google SDK.
- **Request body:**

```json
{
  "model": "gemini-3.7-flash",
  "input": [
    {"type": "video", "uri": "<the YouTube URL>", "processing": "agentic"},
    {"type": "text",  "text": "<question><suffix>"}
  ]
}
```

- **Agentic mode is just `"processing": "agentic"`** on the video part. That answers question 12: the video's "agentic" claim is this one field. For a time range the skill switches to `{"type": "static", "start_offset": "135s", "end_offset": "165s"}`, because offsets are only accepted in static mode.
- **The prompt suffix** (added to every question):
  "Cite an MM:SS (or H:MM:SS) timestamp for every claim about a specific moment. Separate what is seen from what is heard when it matters. If the video does not show or say something needed to answer, say so plainly instead of guessing."
- **The default question** when none is given: "Give a thorough summary of this video: what is shown, any on-screen text or code, and what is said, in chronological order."
- **The answer** is read from `steps[]` entries of type `model_output`, text parts joined. The response also carries `usage.total_tokens`, which the skill prints. A reply with no text is an error.
- **YouTube URL test:** a regex accepting `youtube.com/watch?v=`, `/shorts/`, `/live/`, `youtu.be/`, `m.` and `music.` hosts. Anything else is downloaded and uploaded through the Files API.
- **Errors, by HTTP code:** 401/403 or "api key" in the message → `auth`; 429 → `quota`; 5xx → `service`; other 4xx → `rejected` ("the video may be private, unsupported, or too long"); network → `network`. The key is scrubbed from every message.
- **No retry, no back-off, no rate limiting.** One call, timeout 600 s, and on a failure the skill stops and tells the user. It also does **not** read any quota headers. For a queue we have to build all of that ourselves (question 14 answered).
- It is **one video per call**, even though the API accepts up to 10.
- It does not request structured output (no JSON schema). The answer is free text.

### How it differs from what idea-catcher does

| | **`/watch` skill** | **idea-catcher (`youtube` method, today)** |
|---|---|---|
| **Goal** | An agent answers a question about one video, interactively | A pipeline turns many videos into notes, unattended |
| **Where it runs** | Inside Claude Code, ChatGPT Work or Codex, started by a person | A worker in a service with a queue, jobs and a database |
| **What it reads** | The picture and the sound (Gemini), or sampled frames plus captions (local) | Captions as text only, plus title, channel, counts, chapters (`YoutubeFacts`) |
| **YouTube call** | Gemini: none from our side, Google fetches. Local: yt-dlp | yt-dlp for metadata and captions, from our machine |
| **Metadata** (views, likes, subscribers, upload date, chapters, description) | **None on the Gemini path**, only the answer text. The local path gets title, uploader and duration only | Full set, set by Python, so the LLM cannot invent them |
| **Output** | Free text with timestamps, relayed by the agent | A structured page, checked by free checks (timestamps, tools), with a saved facts file |
| **Blocking / pacing** | None built in. Relies on Google not blocking | A gate: a 10 s pace, a 2-minute gap between calls, a circuit breaker, deferral on block |
| **Channels** | Not at all: `--no-playlist`, one URL at a time | `backfill/channel.py` lists a channel in one flat, paced yt-dlp call, up to 100 videos per listing |
| **Cost control** | None. It prints `total_tokens` after the fact | Profiles with budget caps and a per-run trace |
| **Failure policy** | Never switches engine, asks the user | Defers the note and retries later |
| **Trust in the answer** | Treated as untrusted evidence, but nothing verifies it | Checked against the real transcript |

**In one line:** `/watch` is a good way for a person to *ask a video questions*. idea-catcher is a *pipeline*. They are not rivals. What `/watch` has that we lack is **eyes and ears**: it reads what is on screen and handles videos with no captions. What we have that `/watch` lacks is everything that makes a channel scan safe and checkable.

### What it can add to our solution

1. **A new fact source for the pipeline: a "watch" step through the Gemini API.** Copy the call (about 40 lines): the Interactions endpoint, `processing: agentic`, the prompt suffix. It fits next to the `youtube-gemini` prompt we already have, with no manual chat.
2. **Videos without captions.** Today they are deferred and asked again after a day (`fetched_utc`). Gemini reads the audio, so these no longer need to wait.
3. **Visual facts.** Code on screen, slides, UI demos that the speaker never says aloud. For the Epiaku channel (vibe coding demos) that is real content the transcript misses.
4. **A way out of the YouTube block for the *content*.** The caption fetch is what gets blocked. The Gemini call does not touch YouTube from our machine. The listing and the metadata still do, but those are the cheap calls and the gate already protects them.
5. **Ideas worth borrowing even without Gemini:** a caption-track chooser that prefers the original-language track and records its provenance (manual or automatic, original or translated), which our code does not do (it takes `languages=("en",)`); `--no-playlist` as a guard; and the "if the video does not show or say it, say so" instruction in our own prompts.

### What it cannot add (and the traps)

- **No metadata.** It does not give views, likes, subscribers, upload date or chapters. We still need the yt-dlp call for those, so the block risk is reduced, not removed.
- **No timestamps we can check.** Its timestamps come from the model. Our free check compares timestamps with the transcript, which we would not have for a Gemini-only note. We could still run yt-dlp for captions when allowed, and use Gemini as the fallback or second opinion.
- **Free text, no schema.** We would have to ask for our page structure in the prompt, or add structured output to the call (not checked in the docs).
- **Different backend.** `llm/backends` has only `openai_compatible` and `fake`. A Gemini video call is a new backend with its own request shape, not a profile change. Whether Google's OpenAI-compatible endpoint accepts a YouTube video part is **not checked**.
- **No retry or pacing in the skill**, so none to borrow. The gate we have should wrap the Gemini call too, with an hours-per-day budget (8 h on the free tier).
- **Preview feature and a young third-party skill.** Release 0.3.0 added the Gemini engine on 2026-09-25, three weeks before this check. Copy the call, do not depend on the package.
- **A long video loses detail**, and the skill gives no help for that (a manual `--start`/`--end` only).

### The existing channel lister answers question 4

`backfill/channel.py` already lists a channel or playlist (`@handle`, `/channel/`, `/c/`, `/user/`, `/playlist?list=`) in one flat, paced yt-dlp call, through the same gate, up to 100 videos per listing. So the plan is not "find a way to list a channel": that exists. The new piece is only what happens **per video** after the listing.

## What it could give idea-catcher

- **A third method, `youtube-api`:** URL in, structured findings out. No manual Gemini chat (unlike `youtube-gemini`) and no transcript fetch from our machine (unlike `youtube`).
- **Channel scanning:** loop over a channel's video URLs and clip each one. See [the backfill doc](idea-catcher-youtube-backfill-explained.md) for what already exists.
- **Videos without captions:** Gemini reads the audio, so the "no captions, no summary" gap closes.
- **Questions beyond the transcript:** things shown on screen but not said.

## Open questions (the investigation list)

| # | Question | Why it matters |
|---|---|---|
| 1 | ~~Does the Gemini API accept a YouTube URL?~~ **Answered:** yes, in preview, public videos only, all Gemini models, up to 10 per request from 2.5 on | The whole idea rests on it |
| 2 | ~~Free-tier limit?~~ **Partly answered:** 8 hours of video per day (confirmed). Requests and tokens per minute are not in the docs, they are in AI Studio for our project. At 8 h/day, 200 videos of 15 minutes is 50 hours, so about 6 days. | Fine for a backfill, too slow for a daily scan of many channels |
| 3 | ~~Is bulk use allowed?~~ **Partly answered:** the additional terms say nothing about YouTube or bulk use. The free tier lets Google use the content and have humans read it, the paid tier does not. The general API terms and YouTube's own terms are **not read yet**. | Terms of service and privacy |
| 4 | ~~How do we list a channel's videos?~~ **Answered:** `backfill/channel.py` already does it (flat, paced yt-dlp call through the gate, up to 100 per listing). The listing itself is still a YouTube call, so the block risk stays for that step. | Gemini reads one URL, it does not list a channel |
| 5 | Can the output be forced into our page format (Summary, Chapters, Tips, Channel Application) and carry timestamps? | We need them for the free checks |
| 6 | How accurate is it? Run it on videos we already checked in [the methods comparison](idea-catcher-youtube-methods-comparison.md) and compare to the transcript. | Gemini chat summaries had errors that nothing could catch |
| 7 | Metrics (views, likes, subscribers) are not in the video. Do we still need a YouTube call for them? | Python-set metrics are a strength of the `youtube` method |
| 8 | Which profile and key? A fourth LLM profile with its own key and budget cap (see the LLM notes). | Keys have budget caps |
| 9 | Private, age-restricted or very long videos: what happens? The guide says private videos fail and long ones lose detail. | Failure handling for a queue |
| 10 | Speed: about "a couple of minutes" per video (p. 11). Can calls run in parallel, and what do the rate limits allow? | Decides whether a full channel is hours or days |
| 11 | ~~Does the skill split long videos?~~ **Answered:** not for Gemini, per the README. It sends the whole video, and `--start`/`--end` are only a manual range. Splitting would be our own code. | Quality on long videos against the daily cap |
| 12 | ~~Is there an agentic mode?~~ **Answered:** `"processing": "agentic"` on the video part of an Interactions API request, timestamps come from the prompt. Clipping is static mode only. Still open: whether "87% fewer tokens" holds for our videos (measure `usage.total_tokens`). | The cost of a channel scan |
| 13 | What are the exact free-tier requests per minute and per day for our project? Look in AI Studio (needs the user's key and login). | Sets the pacing of the queue |
| 14 | ~~What does `gemini.py` send and how does it retry?~~ **Answered:** see "The Gemini call, exactly". No retry, no back-off, one video per call. | We build the queue behavior ourselves |
| 16 | Does structured output (a JSON schema) work on the Interactions API with a video part, so the answer fits our page format? | Saves a reformat step and its errors |
| 17 | Does Google's OpenAI-compatible endpoint accept a YouTube video, or does this need a new backend next to `openai_compatible`? | Size of the change in `llm/backends` |
| 18 | Do we use Gemini as the main source, as a fallback when captions are missing or blocked, or as a second opinion next to the transcript? | The methods comparison suggests a transcript-first design with Gemini as fallback. Decide with real runs. |
| 15 | Paid or free for the real scan? Free lets Google use and review the content, caps at 8 h/day, and is a preview. Paid costs about $0.03 to $0.07 per 15-minute video (estimate). | Decides the profile and the budget cap |

## How it would fit in the pipeline

```
channel video list  ->  queue (paced, daily cap)  ->  Gemini API (URL + prompt)
                                                  ->  reformat to page  ->  clippings
```

The existing pacing, the 2-minute gap and the circuit breaker should carry over, with a daily cap in hours instead of a gap.

## Next steps

1. ~~Google's docs~~ **Done (2026-10-15).** Still open: the general API terms and YouTube's terms on bulk use (question 3), and the agentic mode (question 12).
2. ~~Read the `/watch` skill~~ **Done (2026-10-15), complete.** The call, prompt and error handling are written down above.
3. One manual run on a video from the comparison doc and compare the result by hand (this uses a real API key, so the user runs it, not tests). Check the free-tier limits for our project in AI Studio at the same time (question 13).
4. Decide free or paid (question 15), then design `youtube-api` and the channel scan: a channel list source (question 4), a queue with a daily hour cap, back-off on 429, and metrics from a separate source (question 7). Tests use fixtures and mocks.
