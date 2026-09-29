# YouTube transcript fetching: yt-dlp command-line tests

**Started:** 2026-09-29
**Related:** [idea-catcher-youtube-transcript-research.md](idea-catcher-youtube-transcript-research.md) — the `last30days-skill` research that prompted these tests, and where the underlying problem (`facts.py`'s transcript fetch 429ing) is described.
**Purpose:** command-line yt-dlp tests, run from the MacBook on the home network, to diagnose that 429 without touching any code. Log of what was tried and found, kept separate from the research doc so it can keep growing as new tests are run.

**Manual only.** Every command in this doc is for the user to run by hand and paste the result back — never something an agent runs automatically. The IP got rate-limited from real YouTube traffic once already; running more of these calls automatically (in a test, a script, or an agent "checking" a fix) risks making that worse. Any new automated test coverage that comes out of this investigation must use a recorded/mocked fixture (`tests/fixtures/youtube/<video-id>.json`, as `test_facts.py` already does), never a live call.

## Quick command-line test

Run this on the MacBook, on the home network, to check whether `player_client=android` gets a real transcript back instead of a 429. It uses `tcqEUSNCn8I`, the video that was blocked in the pipeline run that started this investigation, so a pass here is a direct before/after comparison:

```bash
yt-dlp --skip-download --write-auto-subs --sub-lang en --sub-format vtt \
  --extractor-args "youtube:player_client=android" \
  -o "%(id)s" "https://www.youtube.com/watch?v=tcqEUSNCn8I"
```

- **Blocked:** yt-dlp exits non-zero, or prints something like `HTTP Error 429: Too Many Requests` or `Sign in to confirm you're not a bot` on stderr, and no `tcqEUSNCn8I.en.vtt` file is written.
- **Working:** yt-dlp exits 0 and `tcqEUSNCn8I.en.vtt` appears in the current directory with real caption text in it (`cat tcqEUSNCn8I.en.vtt` to check).

Drop `--extractor-args "youtube:player_client=android"` to run the same command against yt-dlp's default client cascade, so you can compare directly against what our current `facts.py` gets:

```bash
yt-dlp --skip-download --write-auto-subs --sub-lang en --sub-format vtt \
  -o "%(id)s" "https://www.youtube.com/watch?v=tcqEUSNCn8I"
```

## Tested, 2026-09-29: `player_client=android` fails on the (then-stale) yt-dlp version

Run from the MacBook, home network, against `tcqEUSNCn8I`. Result: **hard failure, no `.vtt` written.**

```
[youtube] tcqEUSNCn8I: Downloading webpage
WARNING: [youtube] No PO Token provided for android client, which is required for working android formats. ...
[youtube] tcqEUSNCn8I: Downloading android player API JSON
WARNING: [youtube] YouTube said: ERROR - Precondition check failed.
WARNING: [youtube] HTTP Error 400: Bad Request. Retrying (1/3)...
... (retries 2/3, 3/3, same error) ...
WARNING: [youtube] Unable to download API page: HTTP Error 400: Bad Request
WARNING: Only images are available for download. use --list-formats to see them
ERROR: [youtube] tcqEUSNCn8I: Requested format is not available. Use --list-formats for a list of available formats
```

**Why:** YouTube now requires a **PO token** (proof-of-origin token) for the android client's player API, which yt-dlp doesn't have by default. Without it, YouTube's player API rejects every request with `Precondition check failed` / `HTTP 400`, and yt-dlp falls back to a format list that only has thumbnail images — no captions, no video formats at all. This is a live, ongoing arms race between yt-dlp and YouTube (tracked in yt-dlp's own GitHub issues); the `last30days-skill` repo's README/docs don't mention needing a PO token, so either they're on an older yt-dlp/YouTube pairing where `android` still worked unconditionally, or their default has since drifted out of date the same way ours had.

**This ruled out `player_client=android` as a safe first fix** on the yt-dlp version in use at the time — it failed outright rather than degrading gracefully, trading a caption-only 429 for a total failure. Turned out to be a stale-yt-dlp artifact rather than a real dead end — see below.

## Tested, 2026-09-29: yt-dlp's default client cascade fails too, not just `android`

Same video, no `--extractor-args` at all (yt-dlp's own default). Result: **also a hard failure, no `.vtt` written** — confirmed by `ls` in the home directory showing no `.vtt` file anywhere.

```
[youtube] tcqEUSNCn8I: Downloading ios player API JSON
WARNING: [youtube] YouTube said: ERROR - Precondition check failed.
WARNING: [youtube] HTTP Error 400: Bad Request. Retrying (1/3)...
... (retries 2/3, 3/3, same error) ...
WARNING: [youtube] Unable to download API page: HTTP Error 400: Bad Request
[youtube] tcqEUSNCn8I: Downloading mweb player API JSON
[youtube] tcqEUSNCn8I: Downloading player fb50cd46
WARNING: [youtube] Falling back to generic n function search
WARNING: [youtube] tcqEUSNCn8I: nsig extraction failed: Some formats may be missing
[info] tcqEUSNCn8I: Downloading subtitles: en
WARNING: Only images are available for download. use --list-formats to see them
ERROR: [youtube] tcqEUSNCn8I: Requested format is not available. Use --list-formats for a list of available formats
```

**What this showed:** yt-dlp's default cascade tries `ios` first (same PO-token `400` as explicit `android`), then falls back to `mweb`, which gets past the player-API call but then fails **signature descrambling** (`nsig extraction failed`) and ends up with only thumbnail-image formats — so format resolution fails before the subtitle write can complete, even though `Downloading subtitles: en` had already started. This was no longer "one client is blocked" — yt-dlp itself was failing to extract usable formats for this video **at all**, from this machine, on the home network, independent of `player_client`. That pointed away from an IP-block explanation and toward **an outdated yt-dlp build** (the `nsig extraction failed` warning specifically means yt-dlp's bundled JS-signature-solving logic doesn't match YouTube's current player code — a known, frequently-recurring class of yt-dlp breakage, fixed by new yt-dlp releases as they ship updated signature-extraction logic).

This also ruled out `ios` and `mweb` (both exercised in this run, both failed) — on the stale binary, at least.

## Tested, 2026-09-29: updating yt-dlp fixes the client/format issues, but a genuine 429 remains

`yt-dlp` was found to be **2024.12.13** — installed via Homebrew (`/opt/homebrew/Cellar/yt-dlp/2024.12.13`), about 9 months stale. `pip`/`uv tool upgrade` weren't the right tool for this install (`pip` isn't even on `PATH`); the right command was:

```bash
brew update && brew upgrade yt-dlp && yt-dlp --version
```

This took it to **2026.8.19**. Re-running the plain (no `--extractor-args`) command against `tcqEUSNCn8I` on the updated binary:

```
[youtube] tcqEUSNCn8I: Downloading webpage
[youtube] tcqEUSNCn8I: Downloading visionos player API JSON
[youtube] tcqEUSNCn8I: Downloading m3u8 information
[info] tcqEUSNCn8I: Downloading subtitles: en
[info] tcqEUSNCn8I: Downloading 1 format(s): 399+251-11
[info] Writing video subtitles to: tcqEUSNCn8I.en.vtt
ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests
```

No more PO-token `400`, no more `nsig extraction failed` — the updated yt-dlp resolves the player API and video formats cleanly (it picked `visionos` on its own) and gets as far as **starting** to write `tcqEUSNCn8I.en.vtt`. But the actual HTTP request for the caption file content itself now returns a plain **`429 Too Many Requests`**, and no `.vtt` is written (`ls *.vtt` → "no matches found").

**Conclusion: the stale-yt-dlp problem and the rate-limit problem were two separate, stacked issues.** Updating yt-dlp fixed the first (client/format resolution, which was masking the second) and exposed the second cleanly: a genuine rate-limit specifically on YouTube's caption-serving HTTP request, reproduced from the home network with a current yt-dlp build. This matches what our own `facts.py` originally reported (429 on the caption fetch, metadata fine) — we're now looking at the real, isolated problem instead of a confounded one. **Keeping yt-dlp current is still worth doing regardless** (it was masking the real symptom and would eventually have caused its own separate failures), but it is not, by itself, the fix for the 429.

## What to test next, now that yt-dlp is current

1. **Retry after a cool-down.** YouTube's per-endpoint rate limits are often time-windowed (minutes to an hour, not permanent) rather than a hard IP ban. Wait 15–30 minutes and re-run the same plain command once, cleanly, without hammering it — a stray sweep of many rapid attempts (ours included, across this session) can itself extend the window.
2. **Re-try `player_client=android` and the other values, now that yt-dlp is current.** The earlier android/ios/mweb failures were confounded by the stale binary (PO-token and nsig bugs that no longer apply). It's worth quickly re-sweeping `android`, `tv`, `tv_embedded`, `web_safari` now, since a client that talks to a different backend path may not be caught by the same 429 as the default cascade:
   ```bash
   for client in android tv tv_embedded web_safari; do
     echo "=== $client ==="
     yt-dlp --skip-download --write-auto-subs --sub-lang en --sub-format vtt \
       --extractor-args "youtube:player_client=$client" \
       -o "test-$client.%(id)s" "https://www.youtube.com/watch?v=tcqEUSNCn8I" 2>&1 | tail -5
   done
   ls test-*.vtt 2>/dev/null
   ```
3. **Try a different, less-recently-hammered video id**, to separate "this specific video/id is rate-limited from repeated testing today" from "this IP/account is broadly rate-limited on the caption endpoint." A fresh video id that has never been requested this session is the cleaner test.
4. **If the 429 persists across a cool-down, other clients, and a fresh video id**, that confirms a real, IP-level throttle on the caption endpoint (not a stale-binary or single-video artifact). At that point see the "Recommended next steps" in [idea-catcher-youtube-transcript-research.md](idea-catcher-youtube-transcript-research.md): the direct-HTTP watch-page scrape (worth trying, since it hits a different URL/request shape than yt-dlp's caption download), SSH-routing to a different network path, or, as a last resort, a Whisper-on-downloaded-audio fallback (`bgutil-ytdlp-pot-provider` doesn't apply here — it's PO-token-specific, not rate-limit-specific).

No code changes have been made as part of these tests — this document is a command-line diagnostic log only.
