# Gemini API Native YouTube Video Understanding (google-genai SDK) — State as of Sept/Oct 2026

## How exactly do you pass a YouTube URL to the Gemini API, and which models support it?

### Takeaway
YouTube URLs are passed as a `file_data`/`file_uri` part alongside a text prompt, either via the classic `client.models.generate_content` call (now labeled "Legacy" in the docs) or the newer `client.interactions.create` API; both accept a bare `https://www.youtube.com/watch?v=...` URL with no file upload step, and Google's official examples use the `gemini-3.8-flash` model as of this writing.

### Cited Findings
- Official "Video understanding" (current) docs give this pattern via `client.models.generate_content`: `types.Part(file_data=types.FileData(file_uri='https://www.youtube.com/watch?v=9hE5-98ZeCg'))` combined with a `types.Part(text=...)`, model `gemini-3.8-flash` — [Video understanding | Gemini API](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding)
- The newer "Interactions API" docs show an alternate, higher-level call shape: `client.interactions.create(model='gemini-3.8-flash', input=[{"type":"text","text":"..."},{"type":"video","uri":"https://www.youtube.com/watch?v=..."}])` — [Video understanding - Interactions API](https://ai.google.dev/gemini-api/docs/interactions/video-understanding)
- The `types.Part.from_uri(file_uri=..., mime_type=...)` classmethod is the documented convenience wrapper for the same underlying `FileData` object; Google Cloud's Vertex AI sample for YouTube summarization uses `Part.from_uri(file_uri="https://www.youtube.com/watch?v=...", mime_type="video/mp4")` — [Use Gemini to summarize YouTube videos | Vertex AI](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/samples/googlegenaisdk-textgen-with-youtube-video)
- Supported models per the (Legacy) Video Understanding page: "Gemini 3.8 Flash, 3.7 Flash, 3.6 Flash, 3.5 Flash Lite, and later models support two video processing modes" (static and "agentic"); the docs also state more broadly that "all Gemini models can process video data" — [Video understanding (Legacy)](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding)
- For models prior to Gemini 2.5, only 1 video is allowed per request; for Gemini 2.5 and later, up to 10 videos per request — [Video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- Only public YouTube videos are supported: "You can only upload public videos (not private or unlisted videos)" — [Video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- The YouTube-URL-as-input feature itself is explicitly flagged in docs as preview-status pricing/quota: "The YouTube URL feature is in preview and is available at no charge. Pricing and rate limits are likely to change." (Note: this line appears in cached/older doc snapshots; given that video is now billed as ordinary input tokens per the pricing page as of Sept 2026, this "free" preview framing may be stale — see pricing section below.) — [Video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- There is a known, reported reliability issue with `Part.from_uri()` returning HTTP 400 "invalid or unsupported file uri" / "Request contains an invalid argument" errors, reproduced against official example code across gemini-2.0-flash, gemini-2.5-pro-preview, and gemini-2.5-flash-preview (filed April 21, 2025, package google-genai==1.11.0, left unresolved in the visible thread). This specific report was for a Google Cloud Storage URI, not a YouTube URL, so its direct applicability to YouTube input is uncertain — [python-genai issue #710](https://github.com/googleapis/python-genai/issues/710)
- A separate, older GitHub issue reports "Gemini API suddenly started failing for Video Understanding" specifically — title suggests a YouTube/video-understanding-specific regression, but full issue content was not retrieved — [python-genai issue #378](https://github.com/googleapis/python-genai/issues/378)

### Inferences
- Google appears to be running two parallel API surfaces in 2026 — the older/legacy `generate_content` + `types.Part`/`FileData` pattern, and a newer `interactions.create` endpoint with a simpler JSON-dict content shape — both documented as currently supporting YouTube URL input with identical model names, suggesting `interactions` is a newer, higher-level API layered on the same backend capability rather than a full replacement (yet).
- The model name `gemini-3.8-flash` appearing consistently across three independently-fetched doc pages suggests the current flagship "Flash" line has progressed through several minor versions (3.5 Lite → 3.6 → 3.7 → 3.8) since a presumed Gemini 3.0 launch, consistent with rapid Flash-tier iteration Google has shown historically; this could not be cross-checked against a changelog in the time available.

### Gaps
- Could not confirm from an authoritative changelog/release-notes page exactly when Gemini 3.x superseded 2.5 as the recommended default, or the full model-version history for 2026.
- Could not independently confirm whether `mime_type` is required, optional, or ignored by the API when passing a YouTube URL via `Part.from_uri` (Vertex sample includes `mime_type="video/mp4"`, but the Gemini Developer API's own `FileData` example omits it entirely).
- Could not determine current resolution status of GitHub issues #710 and #378 (whether fixed by Sept 2026); only the original reports were retrieved.

---

## Does Gemini require captions/subtitles, or can it process video/audio with none? Does it analyze visual frames, or rely on YouTube's auto-captions?

### Takeaway
The Gemini **API's** native video understanding (as documented on ai.google.dev) decodes the actual audio and visual streams of the video — sampling frames at 1 FPS and processing audio directly — and does **not**, per the official technical documentation, depend on YouTube's caption/subtitle track; this is a distinct mechanism from Google's separate consumer-facing "Ask this video" feature inside the YouTube app, which one third-party source describes as caption-dependent. The two should not be conflated, and no official doc page states a captions requirement for the API.

### Cited Findings
- Official docs state Gemini "offer[s] powerful capabilities for understanding video content by processing information from both the **audio and visual** streams" — [Video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- Technical tokenization details describe processing raw decoded media, not caption text: "Each second of video is tokenized as follows: Individual frames (sampled at 1 FPS): If `media_resolution` is set to low, frames are tokenized at 66 tokens per frame. Otherwise, frames are tokenized at 258 tokens per frame. Audio: 32 tokens per second," with audio described elsewhere as processed "at 1Kbps (single channel)" and "timestamps added every second" — [Video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- The docs do not, anywhere retrieved, state that a caption/subtitle track is required as an input or prerequisite for YouTube URL video processing via the API.
- A third-party aggregator site (datastudios.org, dated only "February 3", no year visible) describes a **different, consumer-facing** Google product — the "Ask about this video" feature embedded in the YouTube app — as caption-reliant: "Gemini's 'Ask about this video' feature accesses the YouTube video's closed captions and adds it to your prompt... If something isn't said out loud, it doesn't exist to Gemini... it won't recognize background visuals, on-screen text, or actions shown without narration." The same article separately acknowledges Gemini's "multimodal reasoning about visual cues" for "on-screen diagrams, slides, UI demonstrations" — [Can Google Gemini Summarize YouTube Videos? — datastudios.org](https://www.datastudios.org/post/can-google-gemini-summarize-youtube-videos-video-understanding-and-summary-reliability)
- The same article calls YouTube summarization via Gemini "not a fully deterministic process" and says reliability "drops significantly" when captions are absent, but presents this as qualitative commentary with no controlled test data — [datastudios.org](https://www.datastudios.org/post/can-google-gemini-summarize-youtube-videos-video-understanding-and-summary-reliability)

### Inferences
- Because the API's own technical spec describes frame-by-frame (1 FPS) and raw-audio tokenization rather than any caption-ingestion step, the API path (as opposed to the YouTube app's built-in assistant feature) is architecturally capable of processing videos with zero captions/subtitles — visual-only or audio-only content should still be tokenized and analyzed.
- The datastudios.org claim about caption dependency should be treated as describing a different product (YouTube's in-app Gemini assistant) rather than the `google-genai` SDK / Gemini Developer API video-understanding path this report is scoped to; conflating the two would be a factual error.

### Gaps
- No official ai.google.dev documentation explicitly and affirmatively states "captions are not required" or directly addresses caption dependence/independence for the YouTube-URL-via-API path — this is inferred from the technical tokenization description, not confirmed by a direct statement.
- No independently reproduced test (e.g., a captioned vs. uncaptioned video with identical visual/audio content) comparing API output quality was found in the time available.
- The datastudios.org source's publication year could not be confirmed, and it is a secondary/aggregator source, not Google-official — its specific claims about caption dependency should be weighted accordingly and are likely about the wrong product surface.

---

## Current documented rate limits and quotas for YouTube URL input, free vs. paid tier

### Takeaway
Google does not publish a YouTube-URL-specific RPM/TPM/RPD quota separate from each model's general API rate limits; the one YouTube-specific quota documented is a **daily video-duration cap**: free tier is capped at 8 hours of YouTube video processed per day, while the paid tier has no video-length-based limit. General per-model RPM/TPM/RPD limits (found via AI Studio / rate-limits docs) apply on top of that.

### Cited Findings
- "For the free tier, you can't upload more than 8 hours of YouTube video per day. For the paid tier, there is no limit based on video length." — [Video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- The official Rate Limits doc page does not list per-model RPM/TPM/RPD figures directly in its text content; it states "Rate limits depend on a variety of factors (such as your usage tier) and can be viewed in Google AI Studio," and the retrievable content focused on Batch API enqueued-token limits (e.g., Tier 1 Gemini 2.5 Flash: 3,000,000 batch enqueued tokens; Tier 1 Gemini 2.5 Pro: 5,000,000) and "Priority inference" limits defaulting to "0.3x the standard rate limit" — [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
- No explicit "video input" or "YouTube" line item appears in the rate-limits documentation beyond the 8-hour/day free-tier cap noted above; that cap is documented only on the Video Understanding page, not the Rate Limits page.
- Third-party aggregator figures (not Google-official, unverified against the live AI Studio dashboard) suggest, for context: Gemini 3 Flash free tier ≈ 10 RPM / 250,000 TPM / 1,500 RPD; Gemini 2.5 Pro free tier ≈ 5 RPM / 250,000 TPM / 25 RPD; paid Tier 1 Gemini 2.5 Pro ≈ 150 RPM / 2,000,000 TPM / 1,000 RPD, rising through Tier 2 (1,000 RPM / 5,000,000 TPM / 50,000 RPD) and Tier 3 (2,000 RPM / 8,000,000 TPM); spend-based limits of $10/10min (Tier 1), $50/10min (Tier 2), $200/10min (Tier 3) also apply — [Gemini API Free Tier Rate Limits 2026 aggregator](https://aipromptshub.co/blog/gemini-api-free-tier-rate-limits); these are not Gemini-3.8-Flash-specific and not sourced from ai.google.dev directly, so should be treated as indicative only.

### Inferences
- Because video is billed and rate-limited as ordinary input tokens (see pricing section), a long YouTube video consumes a large chunk of a request's TPM budget — e.g., a 1-hour video at ~100–300 tokens/second is roughly 360,000–1,080,000 tokens of input alone, which is comparable to or exceeds several tiers' entire per-minute token budget in a single call — making the general TPM limit, not a video-specific quota, likely the practical binding constraint for long videos on lower tiers.

### Gaps
- Could not retrieve the exact, current, official per-model RPM/TPM/RPD table for Gemini 3.8 Flash (the model shown in the current YouTube-URL code samples) directly from ai.google.dev; the live table is rendered in AI Studio's dashboard rather than static docs text, so it was not fetchable by URL in this research pass.
- Could not confirm whether the "8 hours of YouTube video per day" free-tier cap is measured in wall-clock video duration processed, cumulative across requests, or counted differently (e.g., per project vs. per API key).
- Could not confirm the still-current accuracy of the "YouTube URL feature is in preview and available at no charge" doc line against the pricing docs, which now describe video billed at standard token rates (see below) — this looks like a stale/contradictory statement possibly left over from an earlier documentation revision.

---

## Current pricing for video input tokens (token-per-second rate)

### Takeaway
Google's official pricing page does not list a separate per-second video price; video input is billed at each model's standard input-token rate, and the *conversion* from seconds of video to tokens is documented on the Video Understanding page: roughly 100 tokens/second of video at default (low) media resolution, or roughly 300 tokens/second at high media resolution (66 or 258 tokens per sampled frame at 1 FPS, plus 32 tokens/second for audio).

### Cited Findings
- Token conversion (official, Video Understanding docs): "Individual frames (sampled at 1 FPS): If `media_resolution` is set to low, frames are tokenized at 66 tokens per frame. Otherwise, frames are tokenized at 258 tokens per frame. Audio: 32 tokens per second," summarized elsewhere on the same page as "approximately 100 tokens per second of video at default (low) media resolution, or approximately 300 tokens per second of video at high media resolution" — [Video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- Official Pricing page treats video the same as text/image input for standard generation models, e.g. Gemini 2.5 Flash listed at "$0.30 (text / image / video)" per million input tokens, with no separate video line item found for Gemini 3.8 Flash in the retrieved content — [Gemini Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- Introductory pricing reported for Gemini 3.8 Flash: "$0.75 / $3.75 per 1M tokens input / output through December 31, 2026," rising to "$1.5 / $7.5 per 1M tokens" standard pricing from January 1, 2027 — [Gemini 3.8 Flash Pricing — MindStudio (third-party)](https://www.mindstudio.ai/blog/gemini-3-8-flash-pricing) (not confirmed directly against ai.google.dev/pricing in this pass; treat as third-party-reported)
- One distinct, unrelated pricing metric found: Gemini's **video-generation** model ("Omni Flash") bills *output* video at "5,792 tokens per million... per second of 720p video," i.e. a $17.50/M-token output rate ≈ $0.101/second of generated video — this is for text/image-to-video generation, not for analyzing an input YouTube video, and should not be confused with the video-understanding input pricing above — [aggregator pricing summary, unverified against ai.google.dev]

### Inferences
- Combining the two official figures: at low resolution and Gemini 3.8 Flash's introductory $0.75/M-input-token rate, one second of analyzed video ≈ 100 tokens ≈ $0.000075/second (~$0.27/hour); at high resolution (~300 tokens/second) that rises to ~$0.000225/second (~$0.81/hour) — plus whatever text prompt/output tokens the call also uses. These are derived calculations, not numbers stated directly by Google, and should be flagged as such if used downstream.
- A frequently-repeated "$0.10 per second of video" figure surfaced by web search is very likely a confusion between the Omni Flash **video-generation** output pricing and **video-understanding** input pricing; it does not match the official token-conversion math for video-understanding input and should not be used for that purpose.

### Gaps
- Could not directly confirm Gemini 3.8 Flash's exact per-million-input-token price on the live ai.google.dev/pricing page text (only reflected via a third-party aggregator); the page fetch returned mostly Gemini 2.5-series figures in the retrievable content.
- Could not confirm whether the free-tier "at no charge" framing for YouTube-URL input (from the Video Understanding page) is still literally true given the pricing page's general token-based billing — this is a likely documentation inconsistency worth flagging to the report writer rather than resolving with speculation.

---

## Determinism/reproducibility of output across repeated calls on the same video

### Takeaway
No official Google documentation or independent test specifically evaluating YouTube-video-understanding output determinism was found; the closest evidence is a Google AI Developer Forum thread reporting **general** (non-video-specific, image-analysis) non-deterministic behavior on gemini-2.5-pro even with a fixed `seed` and low `temperature`, which the community left unresolved/debated rather than confirmed as fixed by Google.

### Cited Findings
- Forum report: with `gemini-2.5-pro`, temperature 0.1, seed 42, thinking budget 256, and a JSON response schema, identical requests analyzing an image (a motorcycle racing number) returned different outputs across repeated calls (`[]` then `["11"]`) — [Google AI Developer Forum thread](https://discuss.ai.google.dev/t/the-gemini-api-is-exhibiting-non-deterministic-behavior-for-the-gemini-2-5-pro-model-it-is-producing-different-outputs-for-identical-requests-even-when-a-fixed-seed-is-provided-along-with-a-constant-temperature-this-behavior-has-been-reliably-rep/101331)
- A community reply in that thread states: "The seed parameter replays the exact randomness, regardless of any sampling parameter," implying that if determinism guarantees held, "seed in conjunction with any sampling parameters will have the same results" — but no official Google staff confirmation or denial is present in the retrieved thread content — [same forum thread]
- General Gemini documentation/community guidance (not video-specific) notes temperature 0 makes the model "mostly deterministic" with "a small amount of variation still possible," and that combining temperature 0 with top_k 1 is a common (though, per the same thread, contested) recommendation for maximizing determinism.
- A third-party aggregator article (datastudios.org, undated year) asserts YouTube summarization via Gemini is "not a fully deterministic process," but supplies no controlled test data, repeated-run comparison, or methodology — [datastudios.org](https://www.datastudios.org/post/can-google-gemini-summarize-youtube-videos-video-understanding-and-summary-reliability)

### Inferences
- Given that (a) the underlying model architecture exhibits documented non-determinism even on simple image-analysis tasks with seed+low-temperature fixed, and (b) video understanding adds further stochastic elements (frame sampling, possible "agentic" on-demand content loading in newer modes), it is reasonable to expect that repeated calls against the same YouTube URL and prompt are *not* guaranteed to produce identical output, but this is an inference from adjacent evidence, not a directly confirmed video-specific finding.

### Gaps
- No source found that specifically ran the same YouTube URL + prompt through the Gemini API multiple times and reported variance/consistency statistics. This is a genuine gap — if the downstream report needs a hard determinism number for video understanding specifically, none exists in public documentation or testing as of this research pass.
- Could not determine whether the newer "agentic" video processing mode (mentioned for Gemini 3.5 Flash Lite and later) is more or less deterministic than "static" mode, since agentic mode dynamically decides what content to load.
- No official Google statement was found addressing the forum-reported seed/temperature non-determinism issue.

---

## Known video length limits for this feature

### Takeaway
Video length limits scale with the model's context window and requested media resolution: per the official docs, a 1M-context model can process videos "up to 3 hours long by default (at low media resolution), or up to 1 hour long at high media resolution"; a separate summary pass on the same/similar doc content stated 2M-context models handle up to 2 hours and 1M-context models up to 1 hour — these two figures are inconsistent and the discrepancy could not be fully resolved in this pass, so both are reported with a flag.

### Cited Findings
- "Models with a 1M context window can process videos up to 3 hours long by default (at low media resolution), or up to 1 hour long at high media resolution." — [Video understanding, verbatim quote pass](https://ai.google.dev/gemini-api/docs/video-understanding)
- A separate fetch/summary of what appears to be the same or a related doc page stated: "Models with a 2M context window can process videos up to 2 hours long, while models with a 1M context window can process videos up to 1 hour long" (resolution setting not specified in that pass) — [Video understanding, initial summary pass](https://ai.google.dev/gemini-api/docs/video-understanding)
- Independent of context-window-based caps, the YouTube-URL-specific constraint is the free-tier "8 hours of YouTube video per day" aggregate cap and "no limit based on video length" on paid tier — [Video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)

### Inferences
- The two conflicting figures are plausibly reconcilable if the "1M → 1 hour" / "2M → 2 hours" figures in the second pass implicitly assumed *high* media resolution (which the first, more detailed quote pins at "1 hour" for 1M context), while the first pass's "3 hours" figure is specifically the *low*-resolution case; i.e., they may not actually conflict once resolution is accounted for, but this reconciliation is an inference, not a confirmed reading of the primary source.

### Gaps
- Could not obtain a single, unambiguous verbatim quote covering both 1M and 2M context window cases at both resolution settings in one pass — the report writer should treat the "3 hours (low-res) / 1 hour (high-res) for 1M context" figure as the more specific and likely more reliable one, given it came from a verbatim-quote-focused fetch, but this should be verified directly against ai.google.dev/gemini-api/docs/video-understanding before being stated as fact in a final deliverable.
- No information found on whether a hard technical maximum (e.g., a fixed number of hours regardless of context window/resolution tricks) exists beyond the context-window-derived limits.
