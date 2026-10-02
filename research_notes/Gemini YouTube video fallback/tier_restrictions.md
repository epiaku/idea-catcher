# Gemini API YouTube Video Input — Access Restrictions and Tier Differences (Free vs. Paid), as of Sept/Oct 2026

## Is YouTube-URL video input available on the Gemini API free tier at all, or does it require a paid/billing-enabled key?

### Takeaway
YouTube-URL video input is available on the free tier — it is not gated behind a billing-enabled key — but the free tier is capped at 8 hours of YouTube video processed per day, versus no length-based limit on paid tiers. Separately, in the EEA, Switzerland, and the UK, the Gemini API Additional Terms restrict API clients to **Paid Services only**, which effectively removes free-tier access (including this feature) for users/clients made available to people in those regions, regardless of whether Google elsewhere offers it at no charge.

### Cited Findings
- "For the free tier, you can't upload more than 8 hours of YouTube video per day. For the paid tier, there is no limit based on video length." — [Video understanding | Gemini API | Google AI for Developers](https://ai.google.dev/gemini-api/docs/video-understanding)
- "The YouTube URL feature is in preview and is available at no charge. Pricing and rate limits are likely to change." — [Video understanding | Gemini API | Google AI for Developers](https://ai.google.dev/gemini-api/docs/video-understanding)
- Free tier qualification is simply "Active project or free trial," with no spend-based rate limit; Tier 1 requires "Set up and link an active billing account" (spend limit $250/month, spend-based rate limit $10 per 10 minutes); Tier 2 requires having paid $100+ with 3+ days since first successful payment; Tier 3 requires having paid $1,000+ with 30+ days since first payment. — [Rate limits | Gemini API | Google AI for Developers](https://ai.google.dev/gemini-api/docs/rate-limits)
- "You may use only Paid Services when making API Clients available to users in the European Economic Area, Switzerland, or the United Kingdom." — [Gemini API Additional Terms of Service](https://ai.google.dev/gemini-api/terms) (per direct fetch of the terms page; a forum thread separately describes this as taking effect under Gemini API Additional Terms effective March 23, 2026 — [Clarification on "Only Paid Services" for EEA/CH/UK — Google AI Developers Forum](https://discuss.ai.google.dev/t/clarification-on-only-paid-services-for-eea-ch-uk/107860))
- The general "Available regions" documentation lists ~195+ supported countries/territories (including EEA countries, Switzerland, and the UK as regions where the *API itself* is available) and does not itself carve out free-vs-paid distinctions on that page — the free/paid distinction for EEA/CH/UK comes from the separate Additional Terms of Service, not the regions-availability page. — [Available regions for Google AI Studio and Gemini API](https://ai.google.dev/gemini-api/docs/available-regions)

### Inferences
- For a developer in the US (or most non-EEA/CH/UK regions) using a free API key, YouTube URL video analysis works today without billing enabled, subject to the 8-hour/day cap.
- For a developer/pipeline whose API client is "made available to users" in the EEA, Switzerland, or UK, the practical requirement is a paid (billing-enabled) key, since the free tier is contractually off-limits there — this is a terms-of-service restriction, not a technical/API-level block description in the docs.

### Gaps
- The exact wording and full context of the EEA/CH/UK "Paid Services only" clause (e.g., whether it depends on the developer's location, the end-user's location, or both) was only confirmed via one direct quote from ai.google.dev/gemini-api/terms and one secondary forum thread; the full terms page was not read in its entirety, so nuances (effective date, exact scope of "making available to users in") are not independently double-checked against a second primary source.

## Does the video need to be public, or can unlisted/private videos be processed (given an authenticated context)?

### Takeaway
The video must be public. Private and unlisted YouTube videos are explicitly not supported by the Gemini API YouTube-URL feature, with no documented authenticated-access exception.

### Cited Findings
- "You can only upload public videos (not private or unlisted videos)." — [Video understanding | Gemini API | Google AI for Developers](https://ai.google.dev/gemini-api/docs/video-understanding)
- A developer forum thread reports a public, embeddable YouTube video still failing to process (400 Bad Request, "contents.parts must not be empty"), with another user confirming the public-only rule in response, indicating that even meeting the "public" requirement doesn't guarantee success for every video (see anecdotal reliability note below). — [Certain YouTube links don't work — Google AI Developers Forum](https://discuss.ai.google.dev/t/certain-youtube-links-dont-work/88350)

### Inferences
- Idea-catcher-style pipelines that rely on unlisted/private videos (e.g., internal drafts, unlisted uploads) cannot use the native YouTube-URL path at all, public or not is a hard requirement, not merely a default.

### Gaps
- No official documentation found describing any OAuth/authenticated-context exception that would allow private/unlisted video access; this appears to not exist as a feature, but the absence of a "no exception exists" explicit statement means this is inferred from omission rather than a positive confirming statement.

## Are there geographic restrictions on this feature (regions where it is unavailable)?

### Takeaway
No restriction specific to the YouTube-URL feature itself was found beyond the general Gemini API region restrictions; however, the EEA/Switzerland/UK "Paid Services only" clause in the Additional Terms of Service functions as a geography-linked restriction on free-tier use of this (and all) Gemini API features in those regions.

### Cited Findings
- "You may use only Paid Services when making API Clients available to users in the European Economic Area, Switzerland, or the United Kingdom." — [Gemini API Additional Terms of Service](https://ai.google.dev/gemini-api/terms)
- General Gemini API/Google AI Studio access is documented as unsupported in certain regions (e.g., mainland China, Hong Kong, Russia are commonly cited as unsupported in secondary sources), separate from the EEA/CH/UK paid-only rule. — [Google AI Studio Not Supported in Your Region? — IPFoxy Blog](https://www.ipfoxy.com/blog/ideas-inspiration/5488) (secondary/aggregator source, not Google-official; flagged as lower-confidence)
- The official "Available regions" page lists ~195+ supported countries/territories without a feature-specific (YouTube-URL) carve-out. — [Available regions for Google AI Studio and Gemini API](https://ai.google.dev/gemini-api/docs/available-regions)

### Inferences
- There is no evidence the YouTube-URL capability is singled out for geo-blocking beyond the platform-wide EEA/CH/UK free-tier restriction; the restriction found is about payment-tier access in those regions, not about the video-understanding feature being disabled outright there.

### Gaps
- Could not confirm from a primary Google source whether mainland China/Hong Kong/Russia exclusions (cited only by a third-party blog) are current and accurate as of 2026; this should be treated as unconfirmed/secondary until corroborated by an official Google page.

## Is there a daily/per-video cap on how many YouTube videos can be analyzed this way (separate from general token-based rate limits)?

### Takeaway
Yes: the free tier is capped at 8 hours of YouTube video processed per day (an aggregate duration cap, not a strict "N videos" count), and there is a separate, tier-independent per-request cap on the number of videos: 1 video per request for models before Gemini 2.5, and up to 10 videos per request for Gemini 2.5 and later models. The paid tier removes the length-based daily cap.

### Cited Findings
- "For the free tier, you can't upload more than 8 hours of YouTube video per day. For the paid tier, there is no limit based on video length." — [Video understanding | Gemini API | Google AI for Developers](https://ai.google.dev/gemini-api/docs/video-understanding)
- "For models prior to Gemini 2.5, you can upload only 1 video per request. For Gemini 2.5 and later models, you can upload a maximum of 10 videos per request." — [Video understanding | Gemini API | Google AI for Developers](https://ai.google.dev/gemini-api/docs/video-understanding)
- These video-specific limits are distinct from — and not documented on — the general Rate Limits page, which covers RPM/TPM/RPD (requests/tokens per minute, requests per day) by tier and model, with no video-specific rows. — [Rate limits | Gemini API | Google AI for Developers](https://ai.google.dev/gemini-api/docs/rate-limits)

### Inferences
- For a bulk-processing pipeline (like an "idea catcher" that ingests many YouTube links per day) on a free-tier key, the practical daily ceiling is video duration (8 cumulative hours), not a video count — a pipeline processing many short clips could hit far more than a handful of "videos/day" before hitting the cap, while a pipeline processing a few long videos could hit the cap quickly.
- The 1-video vs. 10-videos-per-request limit is a payload constraint (how many videos can be referenced in a single API call), independent of daily/tier caps — relevant if the pipeline batches multiple YouTube links into one Gemini request.

### Gaps
- No official per-video (single video) duration/length cap was found in the fetched documentation (see also the length-restrictions question below) — only the aggregate daily 8-hour free-tier figure.
- Not confirmed whether the "8 hours/day" free-tier cap is enforced per API key, per project, or per Google account/billing entity.

## Does the terms of service / usage policy say anything specific about using this feature for automated pipelines or bulk processing (as opposed to interactive use)?

### Takeaway
No language specific to YouTube-URL video processing, automated pipelines, or bulk processing was found in Google's Gemini API Additional Terms of Service, the Abuse Monitoring / usage-policies page, or the Generative AI Prohibited Use Policy references surfaced in this research — the general abuse-monitoring framework (automated scanning for prohibited content, tiered enforcement) applies, but nothing calls out automated/bulk use of the YouTube feature as such.

### Cited Findings
- "By using the Gemini API, you agree to... the Gemini API Additional Terms of Service and Generative AI Prohibited Use Policy," and "Automated systems scan API usage for violations of the Prohibited Use Policy, such as hate speech, harassment, sexually explicit content, and dangerous content... Google may limit your access by adjusting rate limits, temporarily pause your access... or, as a last resort for serious violations, permanently close your access." — [Abuse monitoring | Gemini API | Google AI for Developers](https://ai.google.dev/gemini-api/docs/usage-policies)
- Direct fetch of the Gemini API Additional Terms of Service page found no references to YouTube, automated pipelines, bulk video processing, or scraping policies specific to YouTube content. — [Gemini API Additional Terms of Service](https://ai.google.dev/gemini-api/terms)
- The Abuse Monitoring page itself likewise contains no specific mentions of YouTube video processing, automated/bulk pipelines, or scraping. — [Abuse monitoring | Gemini API | Google AI for Developers](https://ai.google.dev/gemini-api/docs/usage-policies)

### Inferences
- Automated/bulk use of the YouTube-URL feature is not itself prohibited or specially restricted by policy text found; the governing constraints in practice are the documented technical/quota limits (8h/day free tier, per-request video count caps) and the general content-abuse policy that applies to any Gemini API usage, not a bulk-processing-specific clause.

### Gaps
- The full text of the standalone Generative AI Prohibited Use Policy (policies.google.com/terms/generative-ai/use-policy) was not directly fetched/read in this research pass — only referenced secondhand via the Abuse Monitoring page — so a bulk/automation-specific clause there, if any, could not be ruled out with full confidence.

## Are there restrictions tied to the video's copyright/monetization status, or on videos over a certain length?

### Takeaway
No documented restriction tied to a YouTube video's copyright or monetization status, nor any documented per-video maximum length, was found in official Gemini API sources; the only length-related figures found are the free-tier's 8-hours-per-day aggregate cap and the per-request video-count caps (1 or 10).

### Cited Findings
- The official video-understanding documentation's stated limits are: free-tier 8-hours/day aggregate cap, no paid-tier length limit, and 1-video (pre-2.5) or up-to-10-videos (2.5+) per request — with no mention of copyright status, monetization status, or a single-video maximum duration. — [Video understanding | Gemini API | Google AI for Developers](https://ai.google.dev/gemini-api/docs/video-understanding)
- A developer forum thread about a specific public YouTube video failing to process explicitly states the discussion does not address monetization or copyright-related causes, and the root cause of that particular failure was left unresolved/unclear in the thread. — [Certain YouTube links don't work — Google AI Developers Forum](https://discuss.ai.google.dev/t/certain-youtube-links-dont-work/88350)

### Inferences
- Sporadic failures on specific public YouTube URLs (as seen in the forum thread) may be caused by factors Google has not documented (e.g., possibly copyright/Content-ID status, regional video blocks, or other YouTube-side restrictions), but this is speculative — no source confirms copyright/monetization as a cause.

### Gaps
- No official source confirms or denies whether copyrighted/monetized YouTube content, age-restricted videos, or region-locked videos affect processability — this is an open gap given only anecdotal, inconclusive evidence (one forum thread noting an unresolved failure on a public video) was found.
- No official source states a maximum single-video length/duration; only the aggregate daily free-tier cap and per-request video-count caps are documented.
