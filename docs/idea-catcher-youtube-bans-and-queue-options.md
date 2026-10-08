# YouTube IP bans and the queue: how to keep our API hits low

**Analysed:** 2026-10-01
**Why:** the biggest lesson of Stage A was the YouTube IP ban (HTTP 429 on both the transcript API and the yt-dlp caption fallback, see [the research](idea-catcher-youtube-transcript-research.md)). Stage B adds a queue. The question: how does the system run without ever getting banned, and which queue gives us the control (retries, invisible delay, spreading calls over time) to do it?
**Method:** (1) our own code and run logs, (2) yt-dlp's source in our environment (`2026.08.19`), (3) the documentation of the libraries below, read on 2026-10-01. **No YouTube call and no LLM call was made for this analysis.** Numbers that are estimates are marked _estimate_.
**Written for:** the owner, deciding the Stage B queue and the YouTube protections.

## Status: Step 1 is built (2026-10-01)

The protections of Step 1 are in the code and tested (no network). How they differ from the plan below:

| Planned | Built |
| --- | --- |
| A cache of the facts, the existing facts file | **`facts/<video id>.json`** at the root of `idea-bucket` (one file per video, found by id, committed with the run). It **replaces** the `.youtube.json` that used to sit next to each page. A requeue keeps it |
| One extraction per video | **One yt-dlp extraction** gets the info and the caption track. `youtube-transcript-api` is **removed** from the code and the dependencies. Skipping the manifest request is the setting `YOUTUBE_SKIP_MANIFESTS`, **off by default**, because it is untested live: try one `catcher youtube facts` by hand before turning it on |
| Pacing | `YOUTUBE_REQUEST_DELAY_S` (10 s) between requests, also before the caption file, and yt-dlp's own retries lowered to 1 |
| A minimum time between calls | `YOUTUBE_MIN_GAP_S` (**2 minutes**, changed from the 10 minutes of the design on 2026-10-02 to start low and watch for a block) plus up to `YOUTUBE_GAP_JITTER_S` (5 minutes), shared by every run. It was a state file on this machine; since 2026-10-04 it is one row in Postgres (`resources`, `youtube`), shared by the worker and the CLI. **No daily cap** |
| A breaker | A 429, a bot check, `IpBlocked` or `RequestBlocked` opens it for `YOUTUBE_BLOCK_HOURS` (6), then 12, then 24 hours. A working fetch closes it. A block on the caption file is not hidden as "no captions" |
| A document that must wait is deferred | Better: it is **not touched and stays in `inbox/`** with the status `waiting` (before any work starts), so the next run takes it and there is nothing to requeue. `--wait-youtube` sleeps through a short gap (up to `YOUTUBE_WAIT_MAX_S`) so one run can do a batch. `--refresh-facts` fetches again |
| Hardening added on 2026-10-02 (review fixes) | **A damaged gate state fails closed:** the gate closes for the block hours (then a damaged file, since 2026-10-04 a damaged or missing row in Postgres). A value from the far future is capped at 24 hours. A fetch that started before a newer block cannot close the breaker (`blocked_at`, `started_at`). Two fetches that both get a 429 count as one block. **A dry run never calls YouTube** and does not use the gap (`would_fetch`). A clip whose gap closed after its check, or that got a 429, goes **back to `inbox/`** as `waiting`. A private or removed video is remembered for a day. The Stage B gate has to do all of this too |
| A guard against live calls in development | `YOUTUBE_OFFLINE=1` (saved facts still work). `catcher youtube facts` goes through the same gap and breaker |

**Built since in Stage B:** the Postgres queue (B2, B3) and the gate in Postgres (B4; since B4b, 2026-10-04, the only gate: the gate file and `CATCHER_STATE_DIR` are gone). **Built since then:** the pull, run and publish schedules (B6, 2026-10-07). **And:** the backfill import (B8, 2026-10-08): `catcher youtube import` releases the old links a few a day at a low priority (see [Backfill YouTube links](../idea-catcher-how-to-run-stage-b/#backfill)). The text below is the analysis and the design that led here.

## Short answer

1. **A queue does not stop a ban. Making fewer calls does.** The ban came from our own development traffic on 2026-09-28, not from production volume (a few clips a day). The protections that matter are: never fetch the same video twice, never fetch in bursts, and stop completely when YouTube says no. A queue is the place to _enforce_ those rules, not the cause of the fix.
2. **Before Step 1, our code would hit YouTube again on every retry** (fixed: the facts are saved and read back). Facts are fetched fresh on every attempt, the saved facts file is never read back, and a requeue deletes it. In Stage B, a note deferred for an LLM reason would refetch YouTube every `retry_delay` (60 minutes).
3. **Do the protections first, without any queue**, in the existing code: a facts cache, one extraction per video, a **minimum time between YouTube calls** (this is how the calls are dosed), a persistent "blocked until" breaker, and a guard against live calls in development. A call that is not allowed yet is not an error: the document is **deferred** and picked up later. They are small, and they remove most of the risk now.
4. **For the queue: build our own on Postgres**, with the semantics of Azure Storage Queues (invisible delay, a dequeue count, a poison state) plus a small per-resource limiter table. **Celery is overkill and does not give a global rate limit.** Azure Functions cannot be used for the YouTube fetch at all, because YouTube blocks cloud IP ranges. If our own queue grows too big, **Procrastinate** (Postgres-native) is the best fallback.

## What gets us banned

From yt-dlp's own guidance and the transcript library's docs:

- **The limit is per IP address, on request count.** yt-dlp's page on HTTP 429 says it comes from request rate, not bandwidth, mostly during metadata extraction (watch page, player script, API calls for every video). Shared IPs can inherit someone else's block.
- **Retrying makes it worse.** "Every retry restarts the timer." Automatic retry loops continue the requests and extend the block.
- **How long it lasts:** "for a light trip, minutes. For a sustained one, several hours, and occasionally around 24."
- **Cloud IPs are blocked.** The `youtube-transcript-api` README says YouTube blocks most IPs of cloud providers (AWS, Google Cloud, Azure and others), and that static proxies get banned after extended use. Its recommended workaround is rotating residential proxies (paid). Its cookie support is currently not available.
- **A new IP or a VPN is not a fix.** Commercial VPN exit ranges are among the most scrutinised addresses and can lead to permanent bot checks.
- **Our own incident:** heavy live YouTube traffic during development and testing on 2026-09-28 got this machine blocked ([the timeline](idea-catcher-youtube-transcript-cli-tests.md)). It worked again by 2026-10-01.

## How our code hit YouTube before Step 1 (history)

| Finding | Where | Effect |
| --- | --- | --- |
| Facts are fetched on **every attempt** | `facts_for()` in `process.py` calls `svc.facts(vid)` each time | A deferred note, a requeue or a rerun fetches again |
| The saved facts file is **never read back**, only written at success, and **deleted by `--requeue`** | `write_output()` in `publish.py`, `requeue_from_archive()` in `inbox.py` | There is no cache at all |
| **Two paths per clip**: yt-dlp for the info, then the transcript API for the captions. If that fails, yt-dlp **again** (a second extraction) | `fetch_facts()`, `_fetch_transcript()`, `_fetch_transcript_via_ytdlp()` in `facts.py` | More requests than needed, and more on every failure |
| **No pacing**: no `sleep_interval_requests`, no gap between videos | `_extract_info()` only sets `skip_download`, `quiet`, `no_warnings` | Requests go out back to back |
| **yt-dlp retries on its own**: its defaults are `retries=10` and `extractor_retries=3`, and we do not change them | yt-dlp `options.py` | On a 429 yt-dlp may keep trying, and "every retry restarts the timer" |
| **No memory of a block** | nothing stores "YouTube said no" | After a 429 the next run (or hourly in Stage B) tries again |
| Live calls are easy to make by hand | `catcher youtube facts`, the `run pipeline` and `render` recipes in the how-to | Easy to hammer during development |

**Requests per clip, _estimate_:** about **6 to 7 per clip, and 10 or more when the fallback runs**, before yt-dlp's internal retries. [The next section](#which-requests-does-one-video-really-need) lists what each is for and which are needed. I did not measure this. To measure, run `yt-dlp -v` on one video by hand and count the lines, or log requests in the facts code.

**Real volume:** a few clips a day is tiny. A ban needs a burst or a loop: repeated retries, reruns while testing, an hourly retry of the same deferred note, or a batch of clips after a weekend. Those are the cases to design for.

## Which requests does one video really need?

Not all of them. Reading yt-dlp's log and its source, and the transcript library's behaviour (_estimate_, not measured), these are the requests we make for one clip today:

| # | Request | What it gives | Needed? |
| --- | --- | --- | --- |
| 1 | The watch page (yt-dlp) | title, channel, description, counts, chapters, the player settings | **Yes** |
| 2 | The player data, an API call (yt-dlp) | the list of caption tracks and their addresses | **Yes** |
| 3 | A manifest (m3u8) listing the video formats (yt-dlp) | only what is needed to **download** the video | **No.** We never download media. yt-dlp has an option to skip it (the `skip` extractor argument for `hls` and `dash`). Try it by hand once: without formats yt-dlp may need `ignore_no_formats_error` |
| 4 | The caption file (yt-dlp) | the transcript | **Yes** |
| 5 to 7 | The transcript library: its own watch page, player data and caption track | the same transcript again | **No.** It repeats 1, 2 and 4 |
| 8+ | The fallback: a second full extraction | only when the first path failed | Only on failure. It disappears when yt-dlp is the only path |

**Needed: 3 requests per video** (the watch page, the player data, the caption file). Before Step 1 we made about 6 to 7, and 10 or more when the fallback ran. At 10 seconds between requests, a fetch of 3 requests takes about 30 seconds. Dropping the transcript library and skipping the manifest are therefore the two cheapest ways to halve the hits.

## Options to protect against bans

Ordered by value for effort. A to F need no queue.

| | Option | What it prevents | Effort | Verdict |
| --- | --- | --- | --- | --- |
| **A** | **Cache the facts per video id** (read the saved facts before fetching; keep the transcript, title, description and chapters forever; refresh only the counts if asked or older than N days) | Any second fetch for the same video: retries, requeues, reruns, Stage B's hourly retry | Small | **Do first.** Largest win. yt-dlp itself recommends `--download-archive` for the same reason |
| **B** | **One extraction per video**: get the info and the caption track from a single yt-dlp extraction instead of two libraries (and no second extraction in the fallback) | About half the requests per clip, and **every request can be paced** (the transcript library cannot) | Small | **Do.** Fewer requests, one code path, one place to pace |
| **C** | **Pace the requests**: yt-dlp `sleep_interval_requests` (10 s, adjustable) inside a fetch, a minimum gap with jitter between fetches, and lower `retries` and `extractor_retries` so yt-dlp does not hammer. yt-dlp's own `sleep` preset is 0.75 s between requests, 10 to 20 s between downloads and 5 s for subtitles | Bursts, and yt-dlp's silent retries | Small | **Do.** Random gaps are less machine-like than fixed ones |
| **D** | **A minimum time between YouTube calls** (with random jitter), so the needed calls are spread over the day. A daily cap is only an optional, generous failsafe | Bursts, a loop, a batch after a weekend | Small | **Do.** This is the main dosing. See [How the calls are dosed](#dosing) |
| **E** | **A circuit breaker**: on 429, "sign in to confirm you're not a bot", `IpBlocked` or `RequestBlocked`, store `blocked_until` (6 h, then 12 h, then 24 h, reset by a success). Make **no** YouTube call until then. Notes wait, deferred | The "retry restarts the timer" trap | Small, but needs shared state | **Do.** The single most important rule once a block happens |
| **F** | **A guard against live calls in development**: the network fetch only runs when explicitly allowed (an env var), otherwise it uses saved fixtures | The accident that caused the 2026-09-28 ban | Small | **Do.** Cheap insurance |
| **G** | **Gemini clips** (`youtube-gemini`) | | None | **Not a fallback.** The runner never switches by itself. A `youtube` document that cannot be fetched simply waits (deferred). `youtube-gemini` is a **separate kind of capture** you make by hand: you run the prompt in Gemini and clip the chat. You may choose that route for a video you do not want to wait for, but nothing in the runner depends on it |
| **H** | **The official YouTube Data API** for the metadata (title, channel, views, likes, subscribers, description, chapters in the description): `videos.list` costs 1 unit of a 10,000-unit daily quota. But `captions.download` costs 200 units and **only works for the video owner (OAuth)**, so **transcripts are not available this way** | Scraping for the metadata (leaves only the transcript to scrape) | Medium: a Google Cloud key, a new client | **Maybe later.** Safe and official, but it solves only half of the problem |
| **I** | **Rotating residential proxies** (paid) | IP blocks from the home IP | Medium, plus a monthly cost | **Escalate only** if the other steps are not enough. Static proxies get banned |
| **J** | **Cookies from a logged-in account** | Some bot checks | Medium | **Not recommended**: it risks the account, and the transcript library says its cookie support is currently not available |
| **K** | A new IP or a VPN | Nothing durable | | **No** |
| **L** | Run the fetch on **cloud functions** (Azure Functions and so on) | | | **No.** Cloud IP ranges are the ones YouTube blocks |

## Queue options

What we need: about 5 to 50 documents a day, one worker (maybe two), Postgres already planned for metrics, **full control**, a **global limit on YouTube calls**, retries with a delay, a dead state, and recovery when a worker dies mid-job.

| | Extra infra | Retry with backoff | Delay before a retry | Global rate limit | One at a time per resource | Dead / poison | Recovers a crashed worker |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Own Postgres queue** (the plan) | none | we write it | `run_after` | a limiter table, we write it | `SKIP LOCKED` plus a resource key | a status | a lease column, we write it |
| **Procrastinate** (Postgres) | none | `RetryStrategy` (attempts, exponential wait) | scheduled jobs | **no** (we add it) | **yes**: jobs with the same `lock` never run at the same time | failed state | stalled jobs can be retried |
| **PgQueuer** (Postgres) | none | database retries with exponential backoff | yes | `concurrency_limit` is global across workers; a time-based rate limit is **not confirmed** in the docs I read | `concurrency_limit=1` | | heartbeat requeues stalled jobs |
| **Celery** | a broker: Redis or RabbitMQ | `autoretry_for`, `retry_backoff` (2, 4, 8, 16 s…) | `countdown` | `rate_limit` is **per worker instance, not global** | `acks_late` redelivery needs idempotent tasks | no built-in dead-letter | `visibility_timeout` on Redis |
| **Dramatiq** | Redis or RabbitMQ | retries with exponential backoff | yes | **yes**: Concurrent, Bucket and Window limiters on Redis | a limiter of size 1 is a distributed mutex | | |
| **RQ / arq** (_not researched_) | Redis | basic | some | no | no | | |
| **Huey** | none with the SQLite storage | `retries`, `retry_delay` | yes | no built-in limiter | not built in | | |
| **Temporal / Hatchet** | Temporal: at least four containers and about 4 GB; Hatchet: an engine and an API on Postgres | built in | built in | said to be built in (_not checked_) | said to be built in (_not checked_) | yes | yes |
| **Azure Storage Queue + Functions** | a cloud account | `maxDequeueCount` (default 5) | `visibilityTimeout` | not built in | not built in | `<queue>-poison` | automatic |

### Notes on each

- **Own Postgres queue.** A `jobs` table claimed with `FOR UPDATE SKIP LOCKED`. The extra parts are small and we need our own rules anyway: no library knows "YouTube said no, stay away for 6 hours". Cost: we write and test the concurrency code, and need real Postgres in tests (already planned).
- **Procrastinate.** The closest library to our plan: the same technique (`SKIP LOCKED`, `LISTEN/NOTIFY`), no broker, retries with exponential backoff, a `lock` that serialises jobs (`lock="youtube"` gives "one YouTube fetch at a time"), `queueing_lock`, periodic tasks. It has **no** minimum gap and no breaker: we would still write those. Cost: its tables and migrations next to ours, and less direct control of the semantics.
- **PgQueuer.** Similar, and documents a global per-entrypoint `concurrency_limit` and heartbeat recovery. I could not confirm a time-based rate limit from the docs page I read (a search summary mentioned one), so check before counting on it.
- **Celery.** Not a fit. It needs a Redis or RabbitMQ container (the SQLAlchemy and Postgres broker is marked experimental). Its `rate_limit` is per worker, so it is not a global limit. A retry is a message that goes back to the broker, so the state of a job lives outside our database, and redelivery after a crash needs idempotent tasks and the right `visibility_timeout`. Most of its power (chords, canvas, large scale) is unused at our size. Its one real plus is the ecosystem (monitoring).
- **Dramatiq.** The best of the Redis libraries for us: real global limiters. Still needs Redis, and a rate-limit hit is handled by raising an exception and relying on the retry middleware, so it counts as a retry and a breaker still has to be ours.
- **Huey.** The lightest, and no extra container with SQLite, but we plan Postgres anyway, and it has no limiter.
- **Temporal / Hatchet.** Solid, but a different weight class for a few dozen documents a day.

## Azure queues: copy the semantics, not the service

Azure's model is a good template for a resilient queue, and it maps one-to-one on Postgres:

| Azure Storage Queue and Functions | In our Postgres queue |
| --- | --- |
| A message is invisible for `visibilityTimeout` after a failed attempt, which is the delay between retries | `run_after = now + backoff(attempts) + jitter` |
| `DequeueCount`, the number of tries | `attempts` |
| `maxDequeueCount` = 5 (the Functions default), then the message goes to `<queue>-poison` | `max_attempts = 5`, then `status = 'dead'`, listed and requeueable by hand |
| At-least-once delivery, so a handler must be idempotent | Already true for us: overwrite by id, and the calculated names |
| A message taken by a worker that never finishes becomes visible again | `locked_until` with a heartbeat |

**Why not the service itself:** (1) The YouTube fetch cannot run on Azure Functions, because YouTube blocks Azure's IP ranges (the transcript library names Azure explicitly). (2) We could use Azure Storage Queue only as a broker and poll it from our own machine, but our work is local (two git repos, a LAN LLM proxy, a home IP, which is exactly what YouTube tolerates), so the cloud adds credentials, a dependency and cost for no gain. (3) Azure has no rate limit across messages either; we would still build the limiter. **Overkill, yes.**

## Recommended design: protect first, queue second

### Step 1: protections in the current code (no queue)

1. **Cache:** `facts_for()` reads the saved facts for the video id first. Store them in a cache folder (not in `output/`, which a requeue clears), keep the transcript and metadata, refresh the counts only on request or after N days. A requeue and an LLM retry then never hit YouTube.
2. **One extraction** per video for the info and the captions, and no second extraction in the fallback.
3. **Pacing:** `sleep_interval_requests` of 10 s inside a fetch (`YOUTUBE_REQUEST_DELAY_S`), `retries` and `extractor_retries` lowered to 1, and a **minimum time between fetches** with jitter (the dosing, see below).
4. **Breaker with state on disk** (`blocked_until`, the streak), checked before every fetch; a block sets 6 h, then 12 h, then 24 h; a success resets it.
5. **Counters in the log** (fetches today, requests per fetch). An optional daily failsafe, set high, only to stop a runaway loop.
6. **Dev guard:** live YouTube calls need an explicit flag; tests and the default use fixtures.

### Step 2: the Stage B queue, in Postgres

```text
jobs(id, type, payload, status[queued|running|succeeded|dead], attempts, max_attempts=5,
     run_after, locked_until, resource, last_error, ...)
resource_state(resource PK, next_allowed_at, blocked_until, blocked_at, streak)   -- no daily counter: there is no daily cap
```

- **Claim:** pick the oldest job with `run_after <= now`, whose resource is allowed (`next_allowed_at <= now` and `blocked_until <= now`), `FOR UPDATE SKIP LOCKED`. In the **same transaction** set the resource's `next_allowed_at = now + min_gap + jitter`. Two workers can then never break the gap.
- **Transient failure:** `attempts + 1`, `run_after = now + base * 2^attempts + jitter` (the invisible period). After `max_attempts` (5) the job is `dead`.
- **A block is not a job failure:** on a 429 set the resource's `blocked_until` and push the job's `run_after` past it, **without** counting an attempt. No call to YouTube meanwhile.
- **Split the work:** a `youtube.fetch` job (limited resource, idempotent, cached) and a `llm.reason` job (retried freely). An LLM failure then never causes a YouTube call.
- **Other resources get the same table:** FreeLLMApi (the in-call retry of Stage A stays below this), OpenAI (the budget).
- **Starting values, to tune:** a gap of 2 minutes between fetches (plus up to 5 minutes of jitter; first designed as 10 minutes), 10 s between requests inside a fetch, blocks of 6/12/24 h. No daily cap is needed: the gap already limits the day (see the table in [How the calls are dosed](#dosing)).

### Step 3: a decision point

Build the queue core as a small module with tests on **real Postgres** and a **fake clock** (two workers at once, a crash mid-job, a breaker, the gap). If the core grows past about 300 lines or shows concurrency bugs, switch to **Procrastinate** (`lock="youtube"` and our limiter table) instead of maintaining a queue ourselves.

## Tests and checks

- A fake clock, no network. Facts come from the saved fixtures.
- Real Postgres in Docker for the claim logic: two workers claiming at once must respect the gap and the lock.
- A breaker test: a 429 sets `blocked_until` and no further fetch happens until then.
- A cache test: requeue, retry and rerun make zero fetches.
- By hand, once: count the requests per clip (`yt-dlp -v`), to replace the estimate above.

## How the calls are dosed {#dosing}

**What is the limit?** YouTube publishes no numbers. From yt-dlp's own guidance, it is a **rate per IP address over a short window** ("request rate, not bandwidth"), together with how the IP looks (cloud and VPN addresses are treated more strictly). A block lasts minutes to hours, sometimes about a day, and **retrying during a block extends it**. So there is no known "N per day" rule. What triggers a block is many requests close together, so **time between calls is the right control**, not a daily cap. A daily cap would only be a failsafe against a runaway loop.

**One fetch is a short sequence of requests, never in parallel.** To get one video, yt-dlp has to ask YouTube in order (the watch page, the player data, then the caption file), and each step needs the previous one, so they cannot be skipped. The floor is about 3 to 4 requests with a single yt-dlp extraction, and about 6 to 10 today with two tools and a fallback (_estimate_). We do **not** want these back to back. We space them with yt-dlp's `sleep_interval_requests`: **10 seconds by default, set with `YOUTUBE_REQUEST_DELAY_S`** (see [Settings](#settings)). A fetch of 3 requests then takes about 30 seconds. Two limits: the links YouTube returns inside a fetch (the caption URL) expire, which I believe takes hours (not verified), so wait seconds to a minute between the steps of one fetch and not minutes; and **only yt-dlp can be paced**. The transcript library makes its requests back to back with no pacing hook, which is another reason to use yt-dlp for both the info and the captions (option B). The dosing between videos is the quiet time **between** fetches.

| Gap between fetches (before jitter) | At most per day | Good for |
| --- | --- | --- |
| **2 minutes (the default since 2026-10-02)** | about 720, or about 290 with the default jitter (a real cycle of about 5 minutes) | catching up quickly, and watching whether YouTube blocks us |
| 10 minutes (the first design) | about 144 | a few clips a day, with room |
| 30 minutes | about 48 | very careful |

You will rarely get near the limit: with the cache, each video is fetched **once, ever**, so the gap only matters when many new clips arrive together.

**What happens when a call is not allowed yet** (the gap has not passed, or the breaker is open)? It is **not an error**:

1. *(The plan.)* The document is **deferred**, the same way it is when the LLM is down. *As built in Stage A it is better:* the clip stays in (or goes back to) `inbox/` with the status `waiting` and a message such as `YouTube: next call allowed at 14:35` or `YouTube blocked until 20:10`. Where a waiting clip lives in Stage B is still to be decided.
2. The rest of the run goes on (notes, chats and Gemini clips do not need YouTube).
3. A later run picks it up. In **Stage A** that is a run you start by hand (or a cron job you add). In **Stage B** the worker does it automatically: the job simply waits with `run_after` set to the allowed time. That automatic spreading over the day is the real benefit of the queue.
4. For a short wait (under about 2 minutes) the run can simply **wait** instead of deferring.

**The breaker, step by step**
- **Signals that count as a block:** HTTP 429, "Sign in to confirm you're not a bot", `IpBlocked`, `RequestBlocked`. Any other error is an ordinary failure: the document is deferred, with no breaker.
- **On a block:** write `blocked_until = now + 6 hours` (to a small state file), stop all YouTube calls, defer every document that needs YouTube.
- **After the time:** allow **one** probe fetch. If it works, the breaker resets. If it is blocked again, the wait doubles: 12 hours, then 24 hours.
- **Nothing is hammered meanwhile**, because retrying during a block restarts YouTube's timer.

## Settings {#settings}

All adjustable in `.env` (read by `Settings`, with these defaults). Nothing is hardcoded.

| Variable | Default | Meaning |
| --- | --- | --- |
| `YOUTUBE_REQUEST_DELAY_S` | `10` | Seconds between the requests inside **one** fetch (yt-dlp `sleep_interval_requests`) |
| `YOUTUBE_MIN_GAP_S` | `120` | Minimum seconds between the **start** of two fetches (2 minutes) |
| `YOUTUBE_GAP_JITTER_S` | `300` | Up to this many random extra seconds are added to the gap, so the timing is not regular |
| `YOUTUBE_BLOCK_HOURS` | `6` | The wait after the first block. It doubles to 12 and then 24 hours (and stops at 24) |
| `YOUTUBE_OFFLINE` | `0` | `1` = never call YouTube, use saved facts only. For development and tests |
| `YOUTUBE_SKIP_MANIFESTS` | `0` | `1` = skip yt-dlp's request for the video formats (we never use it). **Untested live**, try by hand first |
| `YOUTUBE_WAIT_MAX_S` | `1800` | With `--wait-youtube`: the longest the run sleeps for the gap. A longer wait is left for a later run |
| `YOUTUBE_NEGATIVE_TTL_H` | `24` | A video without captions is asked about again only after this many hours |
| `SCHEDULE_IDEAS_PULL` | `*/30 * * * *` | **When to pull** the `idea-bucket` repo (cron syntax, local time): new captures from Obsidian become visible to the next run. Often, because it is cheap |
| `SCHEDULE_PUBLISH` | `0 6,12,18,23 * * *` | **When to commit and push** the results (`epiaku-docs` pages and the `idea-bucket` mirror), cron syntax. Less often than the pull |
| `SCHEDULE_PIPELINE_RUN` | (planned, see the architecture doc) | When to process the inbox |

## Where the state lives (Stage B)

You suggested Postgres for the state of the documents, and the frontmatter as something you can read when you open a page. **Agreed, and they do different jobs.** The architecture doc already plans `jobs`, `job_items` and `job_events` for this (see [Database and metrics](../idea-catcher-service-architecture/#mvp-database)); `jobs` already has `priority`, `attempts` and `run_after`, and `job_items` has a `status` with `deferred` and `stuck`.

| What | Where | Why |
| --- | --- | --- |
| The content: the note, the archived original, the page | **Files** in `idea-bucket` and `epiaku-docs` | It is the product, and git keeps its history |
| Processing state: status, attempts, next attempt, priority, last error | **Postgres** (`jobs`, `job_items`, `job_events`) | Fast queries ("what is waiting, for how long, what goes first"), and the dashboard reads it |
| Per resource: the next allowed call and `blocked_until` for YouTube | **Postgres** `resource_state` (Stage A: a small state file) | One answer for all workers, so the gap can never be broken by two of them |
| The facts of a video | The facts file next to the page (choice a); a table later if wanted | See the cache section |
| A readable mirror of the status | **Frontmatter** of the working copy in `output/`: `stage`, `stage_reason`, `stage_since` | You see the state when you open the document, and the database can be **rebuilt** from the folders if it is ever lost |

**Rules**
1. **Postgres decides, the frontmatter only reports.** What to pick next and when to retry come from the database, never from reading files.
2. **Writing a file and committing it are separate.** The frontmatter mirror may be written whenever the status or the reason changes; that does not make a commit. A commit is a **policy** of its own (see [When to commit](#when-to-commit)). Keep the mirror small and treat it as a report: values that change every few minutes (attempts, the next attempt time) are stale as soon as they are written, so they live in the database and the dashboard, which are always current.
3. **Which one goes first:** `ORDER BY priority DESC, run_after, created_at` among the jobs that are due. A job whose `run_after` is in the future is invisible until then (like an Azure message with a visibility timeout). A new clip and a requeue you asked for by hand get a high priority, a backfill job a low one, so a new clip never waits behind a long backlog.
4. **In Stage A there is no database**, so the frontmatter and the folders remain the only state, as they are today.

### When to commit and push, and when to pull {#when-to-commit}

Today the run writes files as it goes and makes **one commit per repo at the end of the run**. In Stage B, with fetches spread over hours, **pulling and committing become schedules of their own**, set in `.env` as cron expressions (the same style and the same scheduler loop as `SCHEDULE_PIPELINE_RUN`), so the timing can be changed without code:

| Schedule | What it does | Why it is separate |
| --- | --- | --- |
| `SCHEDULE_IDEAS_PULL` (suggested every 30 minutes) | `git pull --rebase --autostash` of `idea-bucket` | New captures arrive from your phone and Mac at any time, and a pull is cheap. It should run **more often** than the commits |
| `SCHEDULE_PIPELINE_RUN` | Process the inbox: queue the work | Its own cadence. It only sees captures that were pulled |
| `SCHEDULE_PUBLISH` (suggested 4 times a day) | Commit and push the results: pages to `epiaku-docs`, the archive, output and state mirror to `idea-bucket`. It pulls first, so the push is a fast-forward | A few commits a day, not one per job |
| A manual trigger | `catcher publish` now, an API call later | Publish a page when you want it live now |

The existing behaviour stays for hand runs: `run pipeline` commits at the end, and `--push` pushes.

**What a disk crash loses.** The captures are safe: they enter through the `idea-bucket` repo on GitHub, so every received file is backed up even before it is analysed. What is lost is only what was produced since the last push: the archive copies, the working copies and pages, and **the saved YouTube facts**. That last one matters for the bans: after a crash those videos would have to be fetched again, so a daily push at least protects the cache as well. The queue state in Postgres is covered by the nightly `pg_dump` and the Proxmox backup (see the architecture doc).

**Behaviour**
- A missed schedule (the service was down) runs **once**, not once per missed slot, as already planned for the pipeline run.
- A dirty working tree is safe: `pull()` already uses `--rebase --autostash`, and only the pipeline writes to `output/`, so nothing conflicts with Obsidian's pushes.
- Pages (`epiaku-docs`) and the state mirror (`idea-bucket`) may be published on different schedules if that is useful later.
- Postgres is the truth, so the queue, the pick order and the dashboard never wait for a commit.

## The first big batch (backfill)

Today there are many YouTube links that were never analysed (in `epiaku-docs` and elsewhere). After that first batch, the number of new videos a day drops. The design handles both with the same machinery:

- **Getting them in (built in B8, 2026-10-08):** `catcher youtube import` finds the YouTube links in the docs, skips the video ids that already have a page (or a job item, a clip or a backlog row), and keeps the rest in the table `backfill_videos`. `--limit N` releases at most N of them per rolling 24 hours as clip notes marked `backfill: true`, whose jobs run at `BACKFILL_PRIORITY` (`-10`), so new clips (`0`) go first. See [Backfill YouTube links](../idea-catcher-how-to-run-stage-b/#backfill).
- **How long it takes:** a cycle is about the gap (2 minutes), plus an average of 2.5 minutes of jitter, plus about 30 seconds for the fetch, so about 5 minutes. That is **about 290 fetches a day at most** (_estimate_; with a 10 minute gap it would be about 110):

| Backlog | Days to finish (_estimate_) | LLM cost at the video 1 rate (about $0.024 each) |
| --- | --- | --- |
| 100 videos | about 8 hours | about $2.4 |
| 500 videos | about 1.7 days | about $12 |
| 1,000 videos | about 3.5 days | about $24 |

  Longer videos cost more (the whole transcript goes to the LLM), and the OpenAI key has a **budget cap**, so a big backfill should have a budget or a `--limit` per day. The YouTube gap is the bottleneck, not the LLM. **Built:** the `--limit` per rolling 24 hours (default `BACKFILL_DAILY_LIMIT=10`), with a staged start (5, then 20, then 50); no money budget beyond it and the per-backend budget block.
- **All the videos of a channel:** this is **riskier than one video**. Listing a channel makes many requests (yt-dlp's own guidance says hundreds of index pages are what trigger a 429). So list it **once**, with yt-dlp's flat playlist mode, **paced**, store the ids in the database, and only then add the videos as low-priority jobs. **Built in B8:** `catcher youtube import --channel URL --max-videos N` (default 50, at most 100 per run, and a listing must fit in half the gap; one gate slot per channel, no retry). Try it by hand on a small channel first (`--max-videos 20`); it has not been run against YouTube yet, so how many requests a listing needs is still not measured.
- **Nothing changes later.** When the backlog is done, the same gap, breaker and cache just see less traffic.

## Your questions, explained

### 1. The facts cache: where, and for how long

**What it is:** a saved copy of what we got from YouTube for one video: title, channel, description, chapters, the transcript and the counts. With it, the next run does not call YouTube for that video.

**Where, three choices**

| | Where | Consequence |
| --- | --- | --- |
| **a** | The **facts file we already write** (`output/.../*.youtube.json`), and we start **reading it back** and stop deleting it on a requeue | Smallest change. It is already in the `idea-bucket` git repo, so it survives a new machine or a reset of your working copy, and the history shows what YouTube said when. Transcripts of other people's videos stay in your repo (they already do today) |
| **b** | A cache folder on this machine, outside the repos | Simple and not in git. But a ban is per IP, and a cache per machine means each machine fetches once for itself. Lost if you wipe the folder |
| **c** | A Postgres table (Stage B) | One shared place for the service, easy to look at. Needs the database, so it comes later |

**For how long:** the transcript, title, description and chapters hardly change, so keep them. The **counts** (views, likes, subscribers) do change. With scraping, refreshing only the counts is not cheaper than a full fetch, so the simple rule is: **reuse the saved facts, and refetch only when you ask** (for example `--refresh-facts`). The page's "Metrics As Of" line already shows the date of the fetch, so an older number is honest. One more reason to refresh: chapters can appear days after publishing (we saw this).

**Decided: choice a.** Revisit **c** in Stage B.

### 2. A minimum time between calls, not a daily cap
Agreed, and the design above does exactly that. See [How the calls are dosed](#dosing).

### 3. The "Data API" (option H): is it relevant?
It is **not needed now**, and we have not tried it. I meant **YouTube's official Data API v3**, a separate Google service: you create a Google Cloud project and get an API key. It returns title, channel, description, view and like counts and the publish date for a video (and the subscriber count from the channel), at **1 unit per call of a 10,000-unit daily quota**. It does **not** give transcripts of other people's videos (`captions.download` is for the video owner only). So it could replace only the metadata part of the scraping. Today a fetch is about 6 to 7 requests (_estimate_); with the Data API it would be about 3 scraped requests (the transcript) and 2 official calls. Official calls do not risk an IP ban. It costs a Google Cloud setup and a new piece of code. **Skip it for now**; look at it again only if bans continue after the protections are in. It is not the "Gemini" API and not our yt-dlp route.

### 4. Proxies (option I): what, and what are the options
A proxy is a server that makes the request for you, so YouTube sees **its** IP address and not yours. It only matters if **our** IP is blocked or unreliable.

| Type | What it is | For YouTube |
| --- | --- | --- |
| Datacenter proxy | Cheap server IPs | Blocked like any cloud IP |
| Static residential proxy | One rented home-type IP | The transcript library says YouTube bans static proxies after extended use |
| **Rotating residential proxy** | A pool of home-type IPs, a different one per request | The library calls this "the most reliable option". Paid (I have not checked current prices) |

Both tools can use one: yt-dlp has a `proxy` option, and the transcript library has `GenericProxyConfig` and `WebshareProxyConfig`. **Consequences:** a monthly cost, a third party sees our requests, one more thing that can break, and scraping through it is still against YouTube's terms (as the scraping already is). **We are on a home IP, which is the good kind.** So: not now. The order of escalation is: stop calling during a block, call less (the cache and the gap), and only then pay for a proxy.

### 5. Procrastinate: what it is, and do we need it
**What it is:** a Python library that gives us the queue on top of Postgres. We write tasks as normal functions, and it stores the jobs in Postgres tables, hands them to workers (one at a time, safely), retries a failed job with a growing wait, runs jobs later (a delay), runs periodic jobs (like a cron), and puts a "lock" on jobs so that jobs with the same lock never run at the same time (for example `lock="youtube"`).

**What it would save us:** writing and testing the queue core ourselves: the `jobs` table, the safe claim query, retries with backoff, a lease so a crashed worker's job comes back, periodic runs. I estimate about 300 lines plus tests.

**What it would not do for us:** the minimum gap between YouTube calls, the breaker and the cache. We write those either way.

**Do we need it?** No, not to protect against bans (step 1 needs no queue). For Stage B, it is a choice between:

| | Our own queue | Procrastinate |
| --- | --- | --- |
| Control | Full: every rule is ours | Its model and its tables, we adapt to it |
| Work | We write and test the core | We learn the library |
| Maintenance | Ours | A dependency we keep up to date |
| Risk | Concurrency bugs in our code | Surprises from semantics we did not choose |

**Decided: our own queue.** Do step 1. At the start of Stage B, build it as a small prototype with tests on real Postgres. If it gets big or buggy, switch to Procrastinate. You lose little either way, because the jobs only call the functions we already have.

## Decisions (made 2026-10-01)

1. **Facts cache:** choice **a**. Reuse the facts file (read it back, do not delete it on a requeue), and refetch only when asked (for example `--refresh-facts`).
2. **Gap between fetches:** first **10 minutes** plus jitter; **lowered to 2 minutes on 2026-10-02** to start low and monitor for a block (raise `YOUTUBE_MIN_GAP_S` if one happens). **No daily cap.**
3. **Request delay inside a fetch:** **10 seconds**, adjustable with `YOUTUBE_REQUEST_DELAY_S`. The other settings are in [Settings](#settings).
4. **No proxy.** The Data API is not needed now.
5. **Queue:** our **own** queue on Postgres, with Procrastinate only as the fallback if it gets big or buggy.
6. **State:** Postgres for the state and the queue, the frontmatter as a readable mirror written on status changes only.
7. **Backfill:** low-priority jobs behind new clips, with a budget. **Built in B8 (2026-10-08)** with a cap per rolling 24 hours as the budget.

**Still open**
- ~~When to build the Step 1 protections~~ **Done (2026-10-01)**, see the status at the top. Still to do by hand: try `YOUTUBE_SKIP_MANIFESTS=1` once.
- ~~The import command and the channel listing~~ **Built in B8 (2026-10-08).** Still to do by hand: the first channel listing on a small channel.
- ~~A budget for the backfill~~ **The `--limit` cap per rolling 24 hours (B8).** A money budget beyond it and a daily schedule for the release are not built.
- The default times for the pull and publish schedules (the suggested values above can be changed in `.env`).

## Sources

- yt-dlp, [HTTP Error 429 guide](https://yt-dlp.net/errors/http-error-429-too-many-requests), and its `options.py` (`--retries` default 10, `--extractor-retries` default 3, the `sleep` preset), read from the installed `2026.08.19`.
- [youtube-transcript-api README](https://github.com/jdepoix/youtube-transcript-api) (cloud IPs blocked, static proxies banned, rotating residential proxies, cookies not available).
- [Celery tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html), [configuration](https://docs.celeryq.dev/en/stable/userguide/configuration.html), [brokers and backends](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/index.html), and the [rate-limit issue with several workers](https://github.com/celery/celery/issues/5732).
- [Procrastinate](https://github.com/procrastinate-org/procrastinate) and its [queueing locks](https://procrastinate.readthedocs.io/en/stable/howto/advanced/queueing_locks.html).
- [PgQueuer](https://janbjorge.github.io/pgqueuer/).
- [Dramatiq](https://dramatiq.io/reference.html) and its [cookbook](https://dramatiq.io/cookbook.html).
- [Huey](https://github.com/coleifer/huey).
- [Hatchet](https://github.com/hatchet-dev/hatchet) and comparisons with Temporal.
- [Azure Queue trigger for Azure Functions](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-storage-queue) (`maxDequeueCount`, `visibilityTimeout`, poison queue).
- YouTube Data API: [captions.download](https://developers.google.com/youtube/v3/docs/captions/download) (owner-only), the 10,000-unit daily quota and the 1-unit `videos.list` cost.
