---
title: "Idea Catcher: How to Run the API"
linkTitle: "Idea Catcher: Run the API"
description: "How to run the Stage C API: set up API keys, start it on the host or in Docker, use Swagger UI, a curl example for every endpoint, the status codes, the scopes, and what is not built."
weight: 42
type: docs
---

This page shows how to run what Stage C built (2026-10-08): a small **HTTP API** (`catcher api`, the `api` container) that starts runs, publishes and requeues by **putting a job on the queue**, and reads jobs, items and the YouTube gate from Postgres. The API never does the work: the worker does. The worker and the queue are on the [How to Run Stage B page](../idea-catcher-how-to-run-stage-b/), the commands on the [How to Run page](../idea-catcher-how-to-run/), the settings on the [configuration page](../idea-catcher-configuration/), the design in the [service architecture](../idea-catcher-service-architecture/#mvp-api).

## In short

1. Make a key and put it in `.env` as `API_KEYS="mac:read,run:<key>"` (see [Set up API keys](#keys)).
2. Start the stack: `docker compose up -d --build`, or on the host `uv run catcher api` (next to a worker).
3. Open `http://127.0.0.1:8000/docs`, click **Authorize**, paste the key.
4. Or use `curl` with `Authorization: Bearer <key>` (see [Every endpoint](#endpoints)).

**First time?** Follow [The first local test](../idea-catcher-first-local-test/): it resets the test folders, starts everything on your Mac and calls every endpoint from a page in VS Code (`tests/manual/catcher-api.http`).

## Set up API keys {#keys}

Every endpoint except `/health` (and the public `/docs` pages) needs a key. Keys live in one setting, `API_KEYS`, in `.env` (secret, never committed).

- **Format:** entries separated by whitespace, each `name:scope[,scope]:key`. Example: `mac:read,run:<key one> phone:read:<key two>`.
- **Name:** `[a-z0-9_-]`, 1 to 32 characters. Use one name per device so you can tell them apart.
- **Scopes:** `read` and `run` (see [Scopes](#scopes)).
- **Key:** at least 24 characters, printable ASCII, no spaces. A name or a key may appear only once.

Make a key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put it in `.env`:

```bash
API_KEYS="mac:read,run:PASTE-THE-KEY-HERE phone:read:PASTE-ANOTHER-KEY"
```

**Never commit a key** (`.env` is in `.gitignore`; `.env.example` holds only a placeholder). The API never logs a key, never puts one in an error message and never shows one in `/openapi.json`. An empty `API_KEYS`, a malformed entry, a short key or a duplicate makes `catcher api` stop with **exit code 2** before it listens; the message names the entry number (and the name once it is valid), never the key.

To change or remove a key: edit `API_KEYS` and restart the API (`docker compose up -d api`, see below). Keys are read at start. **A key must not contain `$`**: compose interpolates `$` in `.env` values, so such a key would differ inside the container (`secrets.token_urlsafe` and `openssl rand -hex` never make one). Note that the `worker` and `migrate` containers also get `API_KEYS` in real use, because they read the whole `.env` through `env_file`; only the api is limited the other way, to the three variables.

## Start the API {#start}

### On the host

```bash
uv run catcher api                       # 127.0.0.1:8000
uv run catcher api --host 0.0.0.0 --port 8080
```

It needs `API_KEYS` and a `DATABASE_URL` that is **set** (in the environment or in `.env`): the built-in development URL does not count, so a deployed API cannot silently talk to the wrong database. Without them it exits with code 2 and says which is wrong. It needs a migrated database (`uv run catcher db upgrade`) and, to do any work, a worker (`uv run catcher worker`, or the compose `worker`): the API only queues jobs. `--host` defaults to `127.0.0.1` (this machine only); `0.0.0.0` listens on the LAN, so use it on purpose. Stop it with Ctrl-C.

### In Docker

```bash
docker compose up -d --build
```

This starts `db`, `migrate`, `worker` and `api` (see [Run it in Docker](../idea-catcher-how-to-run-stage-b/#docker)). The `api` container waits for the migrations.

- **Without `API_KEYS` in `.env` the `api` container restarts in a loop** (it exits with code 2 each time); `db`, `migrate` and `worker` are not affected. Add the key and run `docker compose up -d api`.
- **Port:** `API_PORT` (default `8000`) is the host port. **Address:** `API_BIND` defaults to `127.0.0.1`, so only this machine can reach the API. To reach it from other devices on the LAN set `API_BIND=0.0.0.0`, on purpose, and see [What is not built](#not-built) (no HTTPS).
- **`API_DOCS` cannot be set in Docker:** the `api` container gets only `DATABASE_URL`, `API_KEYS` and `LOG_LEVEL` from compose, so `/docs` is always on there. On the host, `API_DOCS=false` turns `/docs` and `/openapi.json` off.
- **By-hand runs: stop the api too.** `docker compose stop worker api` before `catcher run pipeline` or `catcher publish` by hand, `docker compose start worker api` after (see [Running by hand](../idea-catcher-how-to-run-stage-b/#docker)). A by-hand run runs any job in the queue, so an API publish during it would push. A POST while no worker runs only queues the job (`202`) until a worker starts, and a queued job makes the next by-hand run refuse (exit 2, an earlier run's job is still queued: the schedule, `catcher jobs add` or the API).
- **The `api` container holds no GitHub token, no LLM key and no repos.** It has no `env_file`. It only reads and writes Postgres, so a leaked API container cannot publish to GitHub or spend LLM budget; the worker, which has those, is not reachable from the API. The smoke run checks this (see [The smoke run](#smoke)).
- The container healthcheck treats any `/health` answer (`200` or `503`) as alive, so the `api` stays healthy while the worker is down.

## Swagger UI {#swagger}

Open `http://127.0.0.1:8000/docs`. Click **Authorize**, paste **only the key** (not the word `Bearer`) and click Authorize; the lock icons close and **Try it out** sends the header for you. `/docs` and `/openapi.json` are public (they hold no data); everything under `/api/v1` still needs the key.

## Every endpoint {#endpoints}

The examples use:

```bash
export CATCHER_KEY="PASTE-THE-KEY-HERE"      # a key with the read and run scopes
export CATCHER_URL="http://127.0.0.1:8000"
```

All endpoints are under `/api/v1`, except `/health`. Times are ISO 8601 (UTC). The response bodies below show the real field names; the values are examples.

Two small things: a request with a trailing slash (`/api/v1/jobs/`) gets a `307` redirect before the key check (no data), and `HEAD /health` is `405` (use `GET` in an uptime monitor).

### `GET /health` (no key) {#ep-health}

```bash
curl -s -w '\n%{http_code}\n' $CATCHER_URL/health
```

```json
{"api": "ok", "database": "ok", "worker": "running"}
```

`200` only when the database answers **and** a worker holds the one-worker lock (the check `catcher health` uses). Otherwise `503` with the failing part named: `"worker": "none"` (no worker holds the lock) or `"database": "down"` with `"worker": "unknown"`. It says nothing about the scheduler: a worker that holds the lock but whose scheduler thread stopped ticking still shows `running`. The body holds no secrets.

### `GET /api/v1/jobs`: list jobs (scope `read`) {#ep-jobs}

```bash
curl -s -H "Authorization: Bearer $CATCHER_KEY" "$CATCHER_URL/api/v1/jobs?status=failed&type=pipeline.run&limit=10"
curl -s -H "Authorization: Bearer $CATCHER_KEY" "$CATCHER_URL/api/v1/jobs?from=2026-10-08T00:00:00Z&offset=50"
```

Filters (all optional): `status` (`queued`, `running`, `succeeded`, `failed`, `cancelled`), `type` (for example `pipeline.run`), `from` and `to` (bounds on `created_at`, inclusive; a time without a zone is UTC), `limit` (1 to 200, default 50) and `offset` (default 0). Newest first. The answer is a page:

```json
{
  "items": [
    {
      "id": "7d1718bf-0000-4000-8000-000000000001",
      "type": "pipeline.run",
      "status": "succeeded",
      "priority": 0,
      "params": {"retry_deferred": true},
      "result": {"...": "..."},
      "error": null,
      "reason": null,
      "attempts": 1,
      "run_after": "2026-10-08T06:00:00Z",
      "created_at": "2026-10-08T06:00:00Z",
      "started_at": "2026-10-08T06:00:01Z",
      "finished_at": "2026-10-08T06:03:40Z"
    }
  ],
  "total": 47,
  "limit": 10,
  "offset": 0
}
```

`total` is the number of jobs that match the filters. Page through with `offset`.

### `GET /api/v1/jobs/{id}`: one job (scope `read`) {#ep-job}

```bash
curl -s -H "Authorization: Bearer $CATCHER_KEY" "$CATCHER_URL/api/v1/jobs/7d1718bf-0000-4000-8000-000000000001"
```

The same fields as a list entry, plus:

```json
{
  "events": [
    {"ts": "2026-10-08T06:00:02Z", "level": "info", "message": "...", "data": null, "item_id": null}
  ],
  "item_counts": {"note": {"published": 3, "deferred": 1}}
}
```

`events` are the first 200 events, oldest first. `item_counts` (document class, then item status, then a count) is filled only for a `pipeline.run` and is `{}` for other jobs. An unknown or malformed id is `404`. This is the endpoint to **poll** after you start something.

### `GET /api/v1/items`: list items (scope `read`) {#ep-items}

```bash
curl -s -H "Authorization: Bearer $CATCHER_KEY" "$CATCHER_URL/api/v1/items?status=stuck"
curl -s -H "Authorization: Bearer $CATCHER_KEY" "$CATCHER_URL/api/v1/items?doc_class=youtube&status=deferred&limit=20"
```

Filters: `status` (`staging`, `waiting_youtube`, `waiting_llm`, `ready`, `published`, `deferred`, `stuck`, `failed`, `duplicate`), `doc_class` (for example `note`, `youtube`), `limit`, `offset`. Most recently updated first. `status=stuck` lists what waits for you (see [Item states, stuck and reconcile](../idea-catcher-how-to-run-stage-b/#item-states)). A page of:

```json
{
  "name": "notes/walks.md",
  "doc_id": "20260930-0815-walks",
  "doc_class": "note",
  "status": "stuck",
  "stage_reason": "gave up after 3 days",
  "stage_since": "2026-10-07T06:00:00Z",
  "profile": "notes",
  "backend": "freellmapi",
  "model": "auto",
  "tokens_in": 812,
  "tokens_out": 240,
  "origin": "inbox",
  "updated_at": "2026-10-07T06:00:00Z",
  "output_path": null
}
```

`name` is the calculated name (`<subfolder>/<file>.md`), the name you give to requeue. File contents, the inbox and failed paths and the saved LLM reply are not served; `output_path` is an absolute path on the worker's disk.

### `GET /api/v1/youtube/gate`: the YouTube gate (scope `read`) {#ep-gate}

```bash
curl -s -H "Authorization: Bearer $CATCHER_KEY" $CATCHER_URL/api/v1/youtube/gate
```

```json
{"state": "gap", "until": "2026-10-08T09:02:10Z", "streak": 0, "next_allowed_at": "2026-10-08T09:02:10Z", "blocked_until": null}
```

The state `catcher youtube gate` shows: `open` (a fetch may go now), `gap` (waiting out the minimum gap between two fetches) or `blocked` (the breaker is open after a YouTube refusal). `until` is when a call is allowed again (`null` when open). A read can repair a damaged gate row to **closed** (fail closed, like the CLI), so even this GET writes in that one case.

### `POST /api/v1/pipeline/runs`: start a run (scope `run`) {#ep-run}

```bash
curl -s -w '\n%{http_code}\n' -X POST -H "Authorization: Bearer $CATCHER_KEY" $CATCHER_URL/api/v1/pipeline/runs
```

```json
{"job_id": "7d1718bf-0000-4000-8000-000000000002", "existing": false}
```

`202` for a new job; **`200` with `"existing": true` and the id of the run that is already queued or running** (whoever queued it: the schedule, the CLI or the API). The job runs in the worker; poll `GET /api/v1/jobs/{job_id}` until `status` is `succeeded` or `failed`.

The body is optional JSON, all fields optional, no other field allowed:

```bash
curl -s -X POST -H "Authorization: Bearer $CATCHER_KEY" -H "Content-Type: application/json" \
  -d '{"profile": "fake", "limit": 3, "retry_deferred": true}' $CATCHER_URL/api/v1/pipeline/runs
```

| Field | Meaning |
| --- | --- |
| `dry_run` | `true`: a **preview** (see below) instead of a real run. Default `false`. |
| `profile` | Use this LLM profile for the run (the same as `--llm-profile`). It is a profile name, not a free-form backend. |
| `limit` | Process at most this many documents. |
| `retry_deferred` | `true`: also retry deferred items now. |

Types are strict (`"3"` is not a limit). Values are checked by the worker's own parameter check, and a refusal is `422` with the reason. That check does not know your profiles: an unknown `profile` name is accepted (`202`) and the job then fails in the worker, so look at the job's `status` and `error`.

**Dry run.** `{"dry_run": true}` queues a read-only `pipeline.preview` job that the worker runs, because the worker holds the repos. It changes nothing (no files, items or gate) and calls no model and no YouTube. Its answer is in the job's `result`:

```bash
curl -s -X POST -H "Authorization: Bearer $CATCHER_KEY" -H "Content-Type: application/json" \
  -d '{"dry_run": true}' $CATCHER_URL/api/v1/pipeline/runs
curl -s -H "Authorization: Bearer $CATCHER_KEY" "$CATCHER_URL/api/v1/jobs/<job_id>"
```

```json
{"report": {"counts": {"would_call_llm": 2, "would_fetch": 1},
            "names": {"items": [{"doc_id": "...", "doc_class": "note", "status": "would_call_llm", "message": "...", "page": null, "tokens_in": null, "tokens_out": null, "llm_saved": false}],
                      "unreadable": {}, "not_found": [], "not_in_archive": []}}}
```

A preview is never answered with a real run and never counts as one. It dedupes only against a queued or running preview with the **same** parameters (`200` and that job id); a preview with other parameters is its own job.

### `POST /api/v1/pipeline/pull`: start a cycle of the chain (scope `run`) {#ep-pull}

```bash
curl -s -w '\n%{http_code}\n' -X POST -H "Authorization: Bearer $CATCHER_KEY" $CATCHER_URL/api/v1/pipeline/pull
```

Queues an `ideas.pull`, exactly as the hourly schedule does: both call the same function (`start_pull`) and use the same dedupe key, so a pull the schedule queued counts as existing, and the other way round. When the pull ends the worker's [chain](../idea-catcher-how-to-run-stage-b/#chain) queues the run, the publish and the follow-ups, so this one request starts the whole cycle. It is the way to simulate the schedule by hand (the local test stack has no schedule on). It answers like a run: `202` `{"job_id", "existing": false}`, or `200` with `"existing": true` when a pull is already queued or running. Its one option is `limit` (a whole number of 0 or more, optional body `{"limit": 3}`): the run the pull starts takes at most that many documents and is the only run of the cycle, and its publish queues no follow-up run, so the rest of the inbox waits. Without it the run takes the whole inbox, and so do the follow-up runs. A body with any other field, or a `limit` that is not a whole number, is `422`. If a pull is already queued or running, the answer is `200` with that pull and its own `limit`, whatever you send now. In a stack without git remotes (the local test stack) the pull does nothing and the cycle works on what is already in the inbox.

### `POST /api/v1/pipeline/publish`: commit and push (scope `run`) {#ep-publish}

```bash
curl -s -w '\n%{http_code}\n' -X POST -H "Authorization: Bearer $CATCHER_KEY" $CATCHER_URL/api/v1/pipeline/publish
```

Queues `pipeline.publish` with `{"pull": true, "push": true}` (the scheduled publish) and answers like a run: `202` `{"job_id", "existing": false}`, or `200` with `"existing": true` when a publish is already queued or running. It takes no options: a body with any field (for example `{"push": false}`) is `422`. This pushes to GitHub **from the worker**, so use it only against repos you mean to publish to.

### `POST /api/v1/items/{name}/requeue`: run one item again (scope `run`) {#ep-requeue}

The name is the calculated name from the items list. It contains a `/` (`notes/walks.md`), which goes into the path as it is:

```bash
curl -s -w '\n%{http_code}\n' -X POST -H "Authorization: Bearer $CATCHER_KEY" \
  $CATCHER_URL/api/v1/items/notes/walks.md/requeue
```

```json
{"job_id": "7d1718bf-0000-4000-8000-000000000003", "existing": false}
```

Queues a `pipeline.run` with `{"requeue": ["notes/walks.md"]}`: the worker takes the capture from `archive/` and processes it again. `404` `item not found` when no item has that name. A requeue is **always its own job** (`202`), never merged into another run.

### `POST /api/v1/backfill/docs`: backfill the YouTube links of the docs (scope `run`) {#ep-backfill-docs}

```bash
curl -s -w '\n%{http_code}\n' -X POST -H "Authorization: Bearer $CATCHER_KEY" -H "Content-Type: application/json" \
  -d '{"dry_run": true}' $CATCHER_URL/api/v1/backfill/docs
curl -s -X POST -H "Authorization: Bearer $CATCHER_KEY" -H "Content-Type: application/json" \
  -d '{"release": 5}' $CATCHER_URL/api/v1/backfill/docs
curl -s -X POST -H "Authorization: Bearer $CATCHER_KEY" -H "Content-Type: application/json" \
  -d '{"release": false}' $CATCHER_URL/api/v1/backfill/docs
```

```json
{"job_id": "7d1718bf-0000-4000-8000-000000000004", "existing": false}
```

Queues a `backfill.docs` job. The worker pulls `epiaku-docs`, finds the YouTube links without a page, adds them to the backlog and, by default, releases the pending ones it scanned as clip notes (up to the worker's `BACKFILL_DAILY_LIMIT`, default 10; the API container cannot see that setting, so the worker resolves it). `"release": false` only adds to the backlog. It is the same work as `catcher youtube import` (see [Backfill YouTube links](../idea-catcher-how-to-run-stage-b/#backfill)). The body is optional JSON, no other field allowed:

| Field | Meaning |
| --- | --- |
| `dry_run` | `true`: scan and report, write nothing. It skips the pull and never releases. Default `false`. |
| `pull` | `false`: do not pull `epiaku-docs` first. Default `true`. Before the pull the worker commits its own pages in `epiaku-docs` locally (as `ideas.pull` does for the idea bucket), so a remote edit of one of them ends the job as `failed`, never in a half-merged stash. |
| `release` | Optional. Omitted or `true`: release up to the default cap. `false` or `0`: release nothing, only add to the backlog. A whole number N from 1 to 100: the cap is N. It releases only the pending videos this call scanned; pending videos it did not scan are untouched. `release: N` is the 24 hour cap: it releases N minus what anyone released in the last 24 hours, and N is at most 100 per request; a second call the same day with the same N releases nothing. Videos over the cap stay `pending`. With the worker setting `BACKFILL_DAILY_LIMIT=0` the default `true` releases nothing (`allowance` 0 in the result). |

`202` for a new job. `200` with `"existing": true` when a real (not dry) docs backfill is already queued or running, whatever its params: your `release` is not merged into it, so `{"release": false}` while a job with the default release is queued gives `200` and that job releases. POST again after that job has ended. A dry run is its own job. A value above 100 for `release`, an unknown field or a wrong type is `422`. Poll `GET /api/v1/jobs/{job_id}`; the `result` holds `found`, `known`, `new`, `released`, `still_pending` and more. When the job released notes (not a dry run, and at least one note), the worker's chain queues a `pipeline.run` by itself; if a `requeue` or `only` run is queued at that moment, the notes wait for the next scheduled pull. `{"release": N}` with nothing new simply releases the pending videos that are linked in the docs: this is the daily release for the docs. To release the backlog of any source with no scan, use [`POST /api/v1/backfill/release`](#ep-backfill-release). Rows found by a docs scan have the same `found_at`, so inside a scan they are released by video id. A video found earlier by a call with `release: false` is released by a later default call that scans it again. The 24 hour cap is shared with the command line.

### `POST /api/v1/backfill/channels`: list YouTube channels (scope `run`) {#ep-backfill-channels}

```bash
curl -s -w '\n%{http_code}\n' -X POST -H "Authorization: Bearer $CATCHER_KEY" -H "Content-Type: application/json" \
  -d '{"urls": ["https://www.youtube.com/@SomeSmallChannel"], "max_videos": 20}' $CATCHER_URL/api/v1/backfill/channels
# add "release": false to only fill the backlog
```

```json
{"jobs": [{"url": "https://www.youtube.com/@SomeSmallChannel", "job_id": "7d1718bf-0000-4000-8000-000000000005", "existing": false}]}
```

Queues one `backfill.channel` job **per URL**. **This calls YouTube: use it by hand on a small channel only** (the first real listing is still untried). The worker lists the channel through the [YouTube gate](#ep-gate) and adds the unknown videos to the backlog.

| Field | Meaning |
| --- | --- |
| `urls` | 1 to 10 channel or playlist URLs, each at most 2048 characters. |
| `max_videos` | 1 to 100, default 50. |
| `release` | Optional. Omitted or `true`: after the listing, release the pending videos of this listing up to the default cap (`BACKFILL_DAILY_LIMIT` of the worker, default 10). `false` or `0`: release nothing. A whole number N from 1 to 100: the cap is N. It releases only the pending videos this call listed, so a video found earlier with `release: false` is released when a default call lists it again; other pending videos are untouched. `release: N` is the 24 hour cap: it releases N minus what anyone released in the last 24 hours, and N is at most 100 per request; a second call the same day with the same N releases nothing. Videos over the cap stay `pending`. Inside one listing the release order is the listing order: newest first for a channel, and a playlist keeps its own order. For a channel, `max_videos: 50` with `release: 20` releases the 20 newest of those 50. Videos found in an earlier call are released before the videos of a newer call. With the worker setting `BACKFILL_DAILY_LIMIT=0` the default `true` releases nothing. Leftovers of a channel are released with [`POST /api/v1/backfill/release`](#ep-backfill-release), with no new listing. |

The jobs run at low priority (`-10`) and wait for the gate: a closed gate only delays a job, and a block during the listing fails it and opens the breaker. `202` when a job is new, `200` only when every job already existed. `release` is not merged into an existing job: if a `release: false` job for the same URL is still queued or running, a default call gets `200` with that job, which releases nothing; POST again after it has ended. A URL that is not a channel or a playlist (a video link, another host) is `422` with that URL in `detail`, and nothing is queued; so is a URL over 2048 characters, a `max_videos` outside 1 to 100 or a `release` above 100. Whitespace around a URL and trailing slashes are removed first, and a URL given twice in one request (also after that clean-up) becomes one job. Channel jobs queued through the API always use priority `-10`: the API container does not read `BACKFILL_PRIORITY` from `.env`. Poll each `job_id`.

### `POST /api/v1/backfill/release`: release the backlog (scope `run`) {#ep-backfill-release}

```bash
curl -s -w '\n%{http_code}\n' -X POST -H "Authorization: Bearer $CATCHER_KEY" -H "Content-Type: application/json" \
  -d '{}' $CATCHER_URL/api/v1/backfill/release
curl -s -X POST -H "Authorization: Bearer $CATCHER_KEY" -H "Content-Type: application/json" \
  -d '{"limit": 5}' $CATCHER_URL/api/v1/backfill/release
```

```json
{"job_id": "7d1718bf-0000-4000-8000-000000000006", "existing": false}
```

Queues a `backfill.release` job. The worker releases the **oldest pending videos of any source** (docs and channels) as clip notes. It does not pull, does not scan, does not list a channel and does not call YouTube. The body is optional JSON, no other field allowed:

| Field | Meaning |
| --- | --- |
| `limit` | Optional. A whole number from 1 to 100, or `null`. Omitted or `null`: the cap is the worker's `BACKFILL_DAILY_LIMIT` (default 10; the API container cannot see it, so the worker resolves it). A number N: the cap is N. |

The 24 hour cap applies and is shared with every other release: `limit` is the cap itself, so the job releases `limit` minus what anyone released in the last 24 hours. A second call the same day with the same `limit` releases nothing. With `BACKFILL_DAILY_LIMIT=0` the default releases nothing. Videos over the cap stay `pending`. The oldest `found_at` goes first; inside one channel listing that is newest video first (a playlist keeps its own order). With an empty backlog the job succeeds and releases nothing (and queues no run).

`202` for a new job. `200` with `"existing": true` when a `backfill.release` job is already queued or running, whatever its `limit`: your `limit` is not merged into it. POST again after it has ended. `0`, `101`, `true`, a string, a decimal or an unknown field is `422`. Poll `GET /api/v1/jobs/{job_id}`; the `result` holds `released`, `repaired`, `still_pending` and `allowance`. When the job released notes, the worker's chain queues a `pipeline.run` by itself, as for the other backfill jobs. The job runs at normal priority and does not wait on the YouTube gate.

### `GET /api/v1/backfill`: the backlog (scope `read`) {#ep-backfill}

```bash
curl -s -H "Authorization: Bearer $CATCHER_KEY" "$CATCHER_URL/api/v1/backfill?status=pending&source=docs&limit=10"
```

```json
{
  "counts": {"pending": 12, "released": 5},
  "videos": {
    "items": [
      {"video_id": "dQw4w9WgXcQ", "source": "docs", "found_in": "notes/walks.md", "status": "pending", "found_at": "2026-10-14T06:00:00Z", "released_at": null}
    ],
    "total": 12,
    "limit": 10,
    "offset": 0
  }
}
```

Filters (all optional): `status` (`pending`, `released`), `source` (`docs`, `channel`), `limit` (1 to 200, default 50), `offset`. `counts` ignores the filters. The videos are ordered oldest `found_at` first, which is the order they are released (the video id breaks a tie).

## Status codes {#status-codes}

| Code | Meaning |
| --- | --- |
| `200` | A read worked. For `POST pipeline/runs`, `pipeline/pull`, `pipeline/publish` and `backfill/docs`, `backfill/release` (and `backfill/channels`, when every job existed): the job **already exists** (queued or running); its id is in the answer and nothing new was queued. |
| `202` | A new job was queued. The worker has not run it yet: poll `GET /api/v1/jobs/{id}`. |
| `401` | No key, a malformed `Authorization` header, an unknown key or two `Authorization` headers. Same answer for every case (`{"detail": "not authenticated"}`). |
| `403` | The key is valid but lacks the scope: `read` for GET, `run` for POST. |
| `404` | Unknown job id (a malformed id is the same), or `requeue` of an unknown item. |
| `413` | A request whose `Content-Length` is over 64 KB. |
| `422` | A bad query value (an unknown `status`, `limit` over 200), a bad body (an unknown or wrongly typed field), a value the worker's own check refuses (the reason is in `detail`), or a backfill URL that is not a channel or playlist. |
| `503` | Postgres is not reachable (every endpoint, `/health` included). `{"detail": "database unavailable"}` on `/api/v1`; the pool recovers when the database returns. `/health` answers its own body instead: `{"api": "ok", "database": "down", "worker": "unknown"}`, or `"worker": "none"` with the database up. |

**The existing-run rule.** A request to start a run answers `200` when *any* whole-inbox `pipeline.run` is `queued` or `running`, queued by the schedule, `catcher jobs add` or the API. A run that works on named items only (a `requeue` from the API, or `only=` from the CLI) is **not** counted as the existing run: starting a run while only such a job is active queues a new full run (`202`). A scheduled run with `retry_deferred` and a run with `limit` do count. Two simultaneous requests make one job: of two POSTs at the same moment one gets `202` and the other `200` with the same id.

## Scopes {#scopes}

- **`read`:** all `GET` endpoints under `/api/v1`.
- **`run`:** all `POST` endpoints.
- **`run` does not imply `read`.** A key that should start a run *and* poll its job needs both: `name:read,run:key`. A `run`-only key gets `403` on `GET /api/v1/jobs/{id}`.

Give each device its own entry, with the smallest scope it needs (a dashboard or a phone: `read`).

## How an API run relates to the schedule {#schedule}

The API and the schedule do the same thing: they put a job on the **same queue** (the Postgres `jobs` table). The worker runs jobs one at a time. An API run is an ordinary `pipeline.run`, with the same handler, the same lock and the same item states as a scheduled one; it shows in `catcher jobs list` and in `GET /api/v1/jobs`. If the schedule's run is already queued or running, your POST gets that job back (`200`) instead of a second run. The API does not start the worker or change the schedules.

## What is not built {#not-built}

Stage C is the small API of the MVP. These are future features, not part of it:

- Metrics aggregation endpoints (`/metrics/*`) and dashboards.
- A schedule that releases the backlog every day (the release itself is `POST /backfill/release`), and cancelling a backfill job.
- Replay, cancel (of a job) and other management endpoints; profiles and schedules endpoints.
- API keys in the database (they are in `.env`, read at start).
- A live progress stream (also for a backfill): you poll `GET /api/v1/jobs/{id}`.
- Rate limiting (a key can call as often as it likes).
- HTTPS: the API speaks plain HTTP and is meant for this machine, the LAN or a VPN only. Do not forward a port to the internet. Put a TLS proxy in front first, or use a VPN, if the traffic leaves a network you trust.

Also true today: `result`, `params` and `error` of a job are returned as the worker stored them. A key with `read` therefore sees file names and may see absolute paths from a failed job's message. Treat a `read` key as private.

## The smoke run {#smoke}

`bash scripts/compose-smoke` (about 8 minutes, needs Docker, no GitHub, LLM or YouTube) runs the whole stack on throwaway copies and, since Stage C, also covers the API (step j). On 2026-10-08 it passed (63 PASS, 0 FAIL). It checks that the `api` container is healthy and published on `127.0.0.1` (checked on the smoke file's port binding, not on `compose.yaml`'s `${API_BIND:-127.0.0.1}`); `/health` is `200` with the worker up and `503` (`"worker": "none"`) with the worker stopped while the container stays healthy; no key and a wrong key are `401`, also on a POST; a key lists the jobs; two POSTs to `pipeline/runs` give `202` and then `200` with the same id, and that job can be read; the container's environment is exactly `DATABASE_URL`, `API_KEYS` and `LOG_LEVEL`, with no token, LLM key or repo variable; an empty `API_KEYS` makes the container exit with code 2; no key appears in any log, image history or image config; and teardown leaves nothing behind. It uses a dummy key and random ports, so it never touches your `.env` or a running `catcher-db`.

It does not cover `API_BIND=0.0.0.0` from another device, the publish and requeue endpoints in Docker, or a real `dry_run` preview against the compose repos (the tests cover the endpoints and the preview handler on a real Postgres, not an API-queued preview in a running worker).
