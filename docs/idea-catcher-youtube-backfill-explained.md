---
title: "Idea Catcher: YouTube Backfill Explained"
linkTitle: "Idea Catcher: Backfill Explained"
description: "How the YouTube backfill works end to end: the two tables that record videos, the channel listing and its limits, max_videos and release, what yt-dlp is asked and when, the API endpoints, and the decisions behind them."
weight: 43
type: docs
---

This page explains the YouTube backfill in one place: how videos are found, how the database remembers them, what the API does, and why it works the way it does. The commands are on the [Stage B page](../idea-catcher-how-to-run-stage-b/#backfill), every endpoint with a curl example is on the [API page](../idea-catcher-how-to-run-api/), and the design record is in the [service architecture](../idea-catcher-service-architecture/).

## In short

- **Two flows, two endpoints (and a third that only releases).** Flow A scans the pages of `epiaku-docs` for YouTube links (`POST /api/v1/backfill/docs`). Flow B lists a YouTube channel or playlist (`POST /api/v1/backfill/channels`). A third endpoint, `POST /api/v1/backfill/release`, lists and scans nothing: it only releases the backlog (see [Release the backlog](#release-backlog)). The two flows differ in where the video ids come from. After that they share the same table, the same release rule and the same pipeline. See [Two flows](#two-flows).
- **Find first, process second.** A found video is first written to the database as `pending`. Only a video that was **released** becomes a clip note, and only then does `yt-dlp` fetch its details and the LLM write the page.
- **Known videos are never processed twice.** A video that already has a page, a job item, a clip or a backlog row is skipped.
- **The API only queues jobs.** The worker does the work, because it has the repos and the YouTube gate. You poll `GET /api/v1/jobs/{id}`.

## Two flows {#two-flows}

The backfill has two separate ways to find videos. Each has its own API endpoint, its own job type in the worker and its own CLI option. Pick the one that matches where your videos are.

### Flow A: videos linked in `epiaku-docs` {#flow-docs}

Use this when you have already collected YouTube links in your notes and pages and want those videos to get a page of their own.

- **Endpoint and job:** `POST /api/v1/backfill/docs`, job type `backfill.docs`. The CLI equivalent is `catcher youtube import`.
- **Where the ids come from:** your own files. The worker pulls `epiaku-docs` and reads every markdown file for video links (`watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`, `/live/`). A playlist or channel link is not a video and is ignored.
- **Calls YouTube:** no. This flow reads only local files and the database, so it needs no gate slot and cannot cause a ban.
- **Body:** `dry_run` (only count, write nothing), `pull` (pull `epiaku-docs` first, default true) and `release` (see [`max_videos` and `release`](#max-and-release)).
- **What it skips:** a video that already has a page in the docs (its `video_id`), a job item, a clip in the idea bucket or a backlog row.
- **Stored as:** `backfill_videos` rows with `source = 'docs'` and `found_in` set to the page that linked the video.
- **Size:** it always scans everything. The number of links does not matter, because storing a row costs nothing. `release` limits how many are processed.

### Flow B: videos of a YouTube channel or playlist {#flow-channel}

Use this when you want the videos of a channel or playlist that you have not linked anywhere yet.

- **Endpoint and job:** `POST /api/v1/backfill/channels`, job type `backfill.channel`, one job per URL. The CLI equivalent is `catcher youtube import --channel URL`.
- **Where the ids come from:** YouTube. The worker runs one flat `yt-dlp` listing of the channel (`/@handle`, `/channel/ID`, `/c/name`, `/user/name`) or playlist (`/playlist?list=...`). It returns only video ids, never video pages.
- **Calls YouTube:** yes, so everything goes through the YouTube gate: one slot per channel, the gap between calls, and the breaker. A closed gate defers the job, a block fails it and opens the breaker.
- **Body:** `urls` (1 to 10), `max_videos` (1 to 100, default 50) and `release`.
- **What it skips:** the same known videos as Flow A.
- **Stored as:** `backfill_videos` rows with `source = 'channel'` and `found_in` set to the channel URL as sent (after trimming spaces and trailing slashes).
- **Size:** `max_videos` limits how many of the newest videos are listed. See [What `max_videos` cannot do](#limits).

### The two flows side by side {#side-by-side}

|                         | Flow A: docs                             | Flow B: channel                                    |
| ----------------------- | ---------------------------------------- | -------------------------------------------------- |
| Endpoint                | `POST /api/v1/backfill/docs`             | `POST /api/v1/backfill/channels`                   |
| Job type                | `backfill.docs`                          | `backfill.channel` (one per URL)                   |
| CLI                     | `catcher youtube import`                 | `catcher youtube import --channel URL`             |
| Video ids come from     | markdown files in `epiaku-docs`          | a YouTube channel or playlist listing              |
| Calls YouTube           | no                                       | yes, through the gate                              |
| Limits how many to find | nothing (it scans all)                   | `max_videos`, at most 100                          |
| Limits how many to process | `release`                             | `release`                                          |
| `source` in the table   | `docs`                                   | `channel`                                          |
| `found_in` in the table | the page that linked the video           | the channel URL                                    |
| Priority                | normal                                   | `-10`, behind new clips                            |
| Can run with the other  | yes, they do not depend on each other    | yes                                                |

### What the two flows share {#shared}

- **One table.** Both write to `backfill_videos`, so a video found by both flows is stored once, and the second flow skips it as known.
- **One release rule.** `release` means the same on both endpoints: it is on by default, `false` turns it off, a number is the cap, and the 24-hour cap is shared by everyone.
- **One pipeline after release.** A released video becomes a clip note in `inbox/clippings/`. The chain runs the same `pipeline.run`, `youtube.fetch`, `llm.reason` and `pipeline.publish` jobs for both. Nothing downstream knows which flow found the video, except `job_items.origin = 'backfill'`.
- **One gate.** The details fetch of a released video (from either flow) and the channel listing (Flow B) use the same YouTube gate, so YouTube never sees two calls at once.

A good first use is to run Flow A with `dry_run: true` to see how many links your docs hold, then Flow A with a small `release`, and only then try Flow B on one small channel.

## Where the videos are recorded {#records}

Two tables, linked by the video id.

| Table             | Holds                                                                                                                                                                                                  | Statuses                                                                                                      |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| `backfill_videos` | Every video the backfill found and has not skipped as known: `video_id`, `source` (`docs` or `channel`), `found_in` (the page, or the channel URL as given), `found_at`, `released_at`                 | `pending` (found), `released` (written as a clip note)                                                        |
| `job_items`       | Every document the pipeline works on, one row each. A released video appears here when the next run stages its clip note. The `doc_id` is the video id (`<id>-gemini` for a Gemini chat about a video) | `staging`, `waiting_youtube`, `waiting_llm`, `ready`, `published`, `deferred`, `stuck`, `failed`, `duplicate` |

`job_items` has `origin = 'backfill'` for these videos but **no channel column**. To see a channel's videos with their processing state, join on the id:

```sql
SELECT b.found_in AS channel, b.video_id, b.status AS backlog,
       i.status AS processing, i.stage_reason
FROM backfill_videos b
LEFT JOIN job_items i ON b.status = 'released' AND i.doc_id = b.video_id
WHERE b.source = 'channel'
ORDER BY b.found_at;
```

Reading the result: no `job_items` match and `pending` means found but not released. `released` with no match means the note sits in the inbox and has not been staged yet. A match shows `published`, `failed`, `deferred` and so on.

Read the tables with `catcher youtube backlog` and `catcher items list --status failed`, or over HTTP with `GET /api/v1/backfill` and `GET /api/v1/items`. No endpoint joins the two tables yet. Only video ids are stored from a channel listing, no titles, until the video is fetched.

### Querying the tables {#querying}

Each table answers a different question.

- **`job_items`: all documents and their status.** One row per document the pipeline works on, backfill or not. Query it to see what is processed (`published`), waiting (`staging`, `waiting_youtube`, `waiting_llm`, `ready`, `deferred`), stuck or failed. The columns that matter are `calculated_name`, `doc_id`, `doc_class`, `status`, `stage_reason`, `stage_since`, `origin` and `updated_at`.
- **`backfill_videos`: all backfill videos, found or released.** Query it to see every video the backfill has found, which of them are still waiting for a release (`pending`) and which were released (`released`). It has no idea what happened after the release.
- **The join: the real status of a released backfill video.** `backfill_videos` says only `pending` or `released`. A `pending` video has no processing status at all, because the pipeline has not seen it, so it has no `job_items` row. Only a **released** video can have one. To know whether a released video is done, waiting or in progress, join `job_items` on the video id (`job_items.doc_id = backfill_videos.video_id`), and only for released rows.

How the combined state reads:

| `backfill_videos.status` | `job_items` row | Meaning |
| --- | --- | --- |
| `pending` | none | Waiting: found, not released yet |
| `released` | none | In progress: the clip note is in the inbox, not staged yet |
| `released` | `staging`, `waiting_youtube`, `waiting_llm` or `ready` | In progress: the pipeline is working on it |
| `released` | `deferred` | In progress, retried automatically: the LLM or YouTube said not now |
| `released` | `published` | Done: the page exists |
| `released` | `stuck` or `failed` | Needs you: requeue it or look at `stage_reason` |
| `released` | `duplicate` | Done: it was a duplicate of an existing page |

These are sketches and have not been run. Check the column names with `\d job_items` and `\d backfill_videos` in `psql` first.

```sql
-- all documents and their status (not only backfill)
SELECT status, count(*) FROM job_items GROUP BY status ORDER BY status;

-- all backfill videos: found or released
SELECT status, source, count(*) FROM backfill_videos GROUP BY status, source;

-- the status of every backfill video; the processing status is joined only for released videos
SELECT b.video_id, b.source, b.found_in, b.status AS backlog,
       i.status AS processing, i.stage_reason,
       CASE
         WHEN b.status = 'pending' THEN 'waiting'
         WHEN i.status IS NULL THEN 'in progress'
         WHEN i.status IN ('published', 'duplicate') THEN 'done'
         WHEN i.status IN ('stuck', 'failed') THEN 'needs you'
         ELSE 'in progress'
       END AS state
FROM backfill_videos b
LEFT JOIN job_items i ON b.status = 'released' AND i.doc_id = b.video_id
ORDER BY b.found_at, b.video_id;

-- how many backfill videos are in each state
SELECT state, count(*) FROM (
  SELECT CASE
           WHEN b.status = 'pending' THEN 'waiting'
           WHEN i.status IS NULL THEN 'in progress'
           WHEN i.status IN ('published', 'duplicate') THEN 'done'
           WHEN i.status IN ('stuck', 'failed') THEN 'needs you'
           ELSE 'in progress'
         END AS state
  FROM backfill_videos b
  LEFT JOIN job_items i ON b.status = 'released' AND i.doc_id = b.video_id
) s
GROUP BY state ORDER BY state;
```

A video whose clip note was released from a Gemini chat has the `doc_id` `<id>-gemini`, so it will not join on `video_id` and shows as `in progress`. Backfill clips are plain YouTube clips, so this does not happen for them in practice.

## The steps in order {#flow}

These are the steps of the two finding flows. They differ only in step 2. A third way, [release the backlog](#release-backlog), skips steps 1 to 4.

1. **Call.** `POST /api/v1/backfill/channels` with `urls` and `max_videos`, or `POST /api/v1/backfill/docs`. The API checks the body (a bad URL is `422`) and puts one job per channel (or one docs job) on the queue. It answers `202`, or `200` with the job that already exists.
2. **List or scan.** The worker takes the job. A channel job asks YouTube for the video **ids** of the newest `max_videos` videos (one flat `yt-dlp` listing, no video page opened). A docs job pulls `epiaku-docs` and reads its markdown files for video links.
3. **Skip known.** Each id is compared with the docs pages, `job_items`, the clips in the idea bucket and `backfill_videos`. Known ones are dropped.
4. **Store.** The unknown ids are written to `backfill_videos` as `pending`.
5. **Release** (see below). Up to the cap, the pending videos of this call are written as clip notes in `inbox/clippings/` and marked `released`.
6. **Process.** The chain sees the released notes and queues a `pipeline.run`. The run stages each note, the `youtube.fetch` job asks `yt-dlp` for the details through the YouTube gate, the `llm.reason` job writes the page, and a publish commits and pushes.

Steps 1 to 4 never fetch a video's details. That starts at step 6, after the video is in the database.

## Release the backlog, with no listing or scan {#release-backlog}

`POST /api/v1/backfill/release` is a third way to release. It finds nothing: it only releases videos that are already `pending`, the oldest first, of any source. There is no pull, no listing and no YouTube call. The body is optional: `{}` uses the default cap, `{"limit": 5}` makes the cap 5 (1 to 100). The 24-hour cap applies as everywhere (`limit` is the cap itself), and the chain queues a `pipeline.run` when it released notes. Use it to drain the leftovers of a channel or of the docs scan without listing anything again.

## `max_videos` and `release` {#max-and-release}

They control two different things.

| Field        | Controls                                                                       | Range                                                                                               |
| ------------ | ------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| `max_videos` | How many videos the channel listing fetches (newest first for a channel; a playlist keeps its own order) | 1 to 100, default 50                                                                                |
| `release`    | How many of this call's pending videos become clip notes and are processed now | omitted or `true`: up to the default cap; `false` or `0`: none; a number `N`: cap `N` (at most 100) |

Rules for `release`:

- **On by default** on both POST endpoints. Add `"release": false` to only fill the backlog.
- **Only this call's videos.** The videos listed by the channel (or found by the docs scan) that are `pending`. A video found in an earlier call with `release: false` is still released if the new call lists it again. Pending videos from other channels or from the docs scan are not touched.
- **The 24-hour cap still applies.** `release: N` is the cap itself: it releases `N` minus everything released in the last 24 hours, whoever released it. Run it twice the same day with the same `N` and the second run releases nothing. Without a number, the cap is `BACKFILL_DAILY_LIMIT` of the worker (default 10). The API container cannot see worker settings, so the worker resolves it.
- **Leftovers stay pending.** Videos over the cap, or left by a call with `release: false`, are kept in `backfill_videos`. A `POST /backfill/docs` releases only the pending videos its scan found, and a `POST /backfill/channels` only the videos that listing returned. To release leftovers of any source with no new listing or scan, use `POST /backfill/release` (see above). `catcher youtube import --release` or `--limit N` on the command line does the same.
- **`BACKFILL_DAILY_LIMIT=0` releases nothing.** With the worker setting at 0 the default (`true`) has an allowance of 0, so nothing is released (the docs result shows `allowance` 0). The worker default is 10.
- **Release order: within a listing, newest first for a channel (a playlist keeps its own order); videos from earlier calls first; docs-scan rows tie-break by video id.** A listing stores its videos with `found_at` a microsecond apart in listing order, and the release goes by oldest `found_at`. `max_videos: 50` with `release: 20` releases the 20 newest of those 50 (for a channel). Rows from a docs scan share one `found_at`, so they go by video id.
- **An existing `release: false` job wins.** If a `release: false` channel job for the same URL is still queued or running, a default call for that URL gets `200` with that existing job, which releases nothing. The API does not merge `release` into an existing job.
- **A dry run never releases.** `POST /backfill/docs` with `dry_run: true` only counts.
- **The CLI is unchanged.** `catcher youtube import` releases only with `--limit` or `--release`.

Examples:

```json
{"urls": ["https://www.youtube.com/@SomeChannel"], "max_videos": 10}
{"urls": ["https://www.youtube.com/@SomeChannel"], "max_videos": 10, "release": false}
{"urls": ["https://www.youtube.com/@SomeChannel"], "max_videos": 50, "release": 20}
```

The first lists 10, stores the new ones and releases them up to the default cap. The second only stores them. The third lists 50 and releases at most 20, the newest first (for a channel).

## The two `yt-dlp` calls {#ytdlp}

There are two very different `yt-dlp` calls.

1. **The details fetch for one video.** This is the expensive one. It only happens for released videos. A video that is already in the database is never added, never released and so never fetched. Saved facts are also reused.
2. **The channel listing.** This only returns video ids, one request per 30 videos. The database cannot be asked before this call, because the ids are what the call returns. So known videos come back and are dropped afterwards.

Every channel listing goes through the YouTube gate: one slot per channel, the gap between calls, and the breaker. A listing must also fit inside half of `YOUTUBE_MIN_GAP_S` (`ceil(N / 30)` pages times `YOUTUBE_REQUEST_DELAY_S`), or the job fails and names the largest `max_videos` the settings allow. A closed gate **defers** the job, a block fails it and opens the breaker, and `YOUTUBE_OFFLINE=1` refuses it. Channel jobs run at priority `-10`, so new clips go first.

## What `max_videos` cannot do {#limits}

- **No start position.** The listing asks for "the first N" (`playlistend`) and has no offset. A second run with the same `max_videos` lists the same top videos, finds them all known and adds nothing new to the backlog (any of them still `pending` is released by default). This is the correct and cheap result.
- **Going deeper.** Raise `max_videos` (10, then 20, then 30). Each run adds only the new slice. Up to 30 videos is one page request, so it costs almost nothing.
- **Ceiling.** The newest 100 videos are the most one channel listing can reach. Older videos need a start offset, which is not built.
- **Re-requested pages.** Each run asks for the earlier pages again. This is a small waste (at most a few page requests, paced by the gate) caused by the missing offset, not a design goal.

## What `max_items` is {#max-items}

There is no `max_items` on the docs endpoint, on purpose. The docs scan reads every page and stores every new link in `backfill_videos`, which costs only database rows and no YouTube call. To limit how many get **processed**, use `release: N`. The remaining links stay `pending` and a later call releases them.

## The decisions {#decisions}

| Decision                                                                               | Why                                                                                                      |
| -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| The API only queues jobs                                                               | Its container has no repos, no YouTube access and no LLM key                                             |
| `backfill.docs` and `backfill.channel` are separate job types, one job per channel URL | A deferral or failure of one channel must not redo the others                                            |
| Channel jobs wait on the YouTube gate resource                                         | The claim skips them while the gate is closed, so one call at a time                                     |
| Channel jobs run at priority `-10`                                                     | New clips are never delayed by a backfill                                                                |
| Release is on by default, `false` turns it off, the 24-hour cap applies                | A call to check 10 videos should process them, without a flood or runaway LLM cost                       |
| `backfill.release` releases the oldest pending videos of any source, with no listing or scan | Leftovers need no new YouTube listing. The same 24-hour cap applies; `limit` is optional |
| A listing is stored in its own order (`found_at` a microsecond apart); a channel listing is newest first, a playlist keeps its own order | The release order (oldest `found_at`) then releases a channel's newest video first |
| Release takes only this call's pending videos                                          | A call must not process older, unrelated backlog                                                         |
| `release` is at most 100 at the API                                                    | One key must not turn the whole backlog into notes at once                                               |
| A succeeded backfill job that released notes makes the chain queue a `pipeline.run`    | The notes are processed without a second call. A dry run, or a job that released nothing, queues nothing |
| The docs job commits the worker's pages before it pulls `epiaku-docs`                  | The same as `ideas.pull`, so the pull never stashes the worker's own changes                             |
| Channel URLs are normalized by trimming spaces and trailing slashes only               | The same channel written two ways is one job                                                             |

## Not built {#not-built}

- A start offset for a channel listing, to pass the 100-video ceiling and avoid re-requested pages.
- A `max_items` field on the docs endpoint.
- An endpoint that joins `backfill_videos` and `job_items` per channel (counts of pending, released, published, failed).
- A schedule that releases every day. The daily release is one call: `POST /backfill/release {"limit": 10}` releases the oldest pending videos of any source.
- Cancelling a backfill job, and a progress stream. Poll `GET /api/v1/jobs/{id}`.
- A real YouTube channel listing has never run against YouTube. Try one small channel by hand (`max_videos: 10`) and look at `catcher youtube gate` before and after.
