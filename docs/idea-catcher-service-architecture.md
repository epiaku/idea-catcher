---
title: "Idea Catcher Service: Architecture"
linkTitle: "Idea Catcher Service"
description: "Architecture of the Python idea catcher service, split into a simple MVP (scheduled and API-triggered processing of idea-bucket docs through a Postgres job queue, with LLM profiles and metrics) and future features (React dashboards in the Hugo site, a YouTube summary endpoint, more LLM backends)."
weight: 20
type: docs
---

The [Idea Catcher pipeline](../idea-catcher-pipeline/) page describes the **flow**: capture in Obsidian, sync to the `idea-bucket` repo, summarize, publish. This page describes the **software** that runs that flow: a small Python service with a job queue, a worker, a database for metrics and an API.

The design has two parts:

- **[Part 1: MVP](#mvp)** — only what is needed to process the docs from `idea-bucket` reliably: a scheduler, a queue, a worker, LLM profiles, metrics in Postgres and a minimal API. We build this first.
- **[Part 2: Future features](#future)** — React dashboards inside the docs site, the YouTube summary endpoint, more LLM backends, management endpoints and more. The MVP is shaped so these can be added **without rewriting it**.

## 📝 Summary {#summary}

- **One job model for everything.** A schedule, an API call and (later) a dashboard button all do the same thing: they put a **job** on a **queue** in Postgres. A worker picks up jobs and runs them. The API never waits for long work. It returns a `job_id` straight away.
- **The LLM step is its own job type (`llm.reason`)** with **LLM options in the message**: a named **profile** that sets the API provider (backend) and the model. One generic `reason()` function serves every document type.
- **MVP backends, all API calls with a key:** **FreeLLMApi** for short notes (profile `notes`) and the **OpenAI API** for AI chats and web clips (profile `clippings`) and for both YouTube classes (profile `youtube`). There is **no CLI and no subscription** (`claude -p` was unreliable and is removed), so the service runs 24/7 on the Mac, on Proxmox or in the cloud. **No fallback between providers.** A failed LLM job is logged and retried on the next run. After 3 failed days the note is flagged **stuck**.
- **One class, one prompt, one LLM call.** Every document class has its own prompt file and profile, and `reason()` makes exactly one call per document — including the two YouTube classes, where the transcript and facts Python fetched are the source of truth given to that one call. See [One class, one prompt, one call](#mvp-one-prompt-per-class).
- **Metrics from day one.** Every run and every processed doc is a row in Postgres: when, which type, which model, how many tokens, warnings and errors. The future React dashboard only has to read what is already there.
- **Same setup everywhere.** One `compose.yaml` runs the stack (`db`, `api`, `worker`) on the Mac for testing and in a Proxmox LXC in production. A manual `deploy.sh` ships it.
- **The docs site stays as it is.** The service commits pages to `epiaku-docs` `main`. You still deploy the site manually with `deploy.sh`.

## 📑 Table of Contents {#toc}

1. [Summary](#summary)
2. [Requirements](#requirements)
3. [Decisions](#decisions)
4. [MVP vs. Future at a Glance](#glance)
5. [Part 1: MVP](#mvp)
   - [Architecture](#mvp-architecture) · [Components](#mvp-components) · [Jobs & Queue](#mvp-jobs) · [LLM Step & Profiles](#mvp-llm) · [Processing Flow](#mvp-flow) · [Scheduling](#mvp-scheduling) · [Database & Metrics](#mvp-database) · [API](#mvp-api) · [Security](#mvp-security) · [Hosting & Deploy](#mvp-hosting) · [Repo Layout](#mvp-layout) · [How to Run](#mvp-run) · [Testing](#mvp-testing) · [Build Steps](#mvp-steps)
6. [Part 2: Future Features](#future)
   - [React Front End in Hugo](#f-frontend) · [YouTube Summary Endpoint](#f-youtube) · [More LLM Backends](#f-llm) · [Dashboard Metrics API](#f-metrics) · [Management API](#f-management) · [Scaling Out](#f-scaling) · [Other Additions](#f-other)
7. [Options Considered](#options) (including [Database options](#db-options))
8. [Gaps, Flaws & Risks](#risks)
9. [Open Questions](#open-questions)

---

## 🎯 Requirements {#requirements}

| #   | Requirement                                                                                                                                                                                         | Part   | What it means for the design                                                                       |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | -------------------------------------------------------------------------------------------------- |
| R1  | **Scheduled runs** a few times a day: look only at `idea-bucket/inbox/` for work. When work on a document starts, move it out of `inbox/`: an untouched copy to `archive/` and a working copy to `output/`, which becomes the final page. Write pages to `epiaku-docs` under `hugo/content/en/docs/idea-bucket/<destination>/` (`notes`, `youtube` or `web-clips`). | MVP    | A scheduler plus a `pipeline.run` job. Git access to both repos.                                   |
| R2  | **An API** to send commands and read information.                                                                                                                                                   | MVP (minimal) → Future (full) | FastAPI with an OpenAPI schema. The MVP only starts runs and reads runs and items.        |
| R3  | **Start a run from the API**, as well as on a schedule.                                                                                                                                             | MVP    | Both paths put the same job on the queue. Only one pipeline run at a time.                         |
| R4  | **Persistent metrics** per run: when it ran, how many docs of each type, warnings and errors.                                                                                                       | MVP (stored) → Future (dashboard endpoints) | `jobs`, `job_items` and `job_events` tables.                          |
| R5  | **Configurable LLM step:** provider and model set **per message** through three profiles (`notes`, `clippings`, `youtube`), one generic function for all doc types.                                                                         | MVP    | `llm.reason` jobs with LLM options and named profiles.                                             |
| R6  | **Run locally and on Proxmox the same way**, deploy easily.                                                                                                                                         | MVP    | Docker Compose on both, a manual `deploy.sh`.                                                      |
| R7  | **A YouTube summary endpoint:** send a URL, get summary markdown.                                                                                                                                   | Future | A `youtube` module that reuses the MVP's YouTube facts and LLM step.                               |
| R8  | **React front ends inside the Hugo docs site** (dashboards, trigger jobs, YouTube), room for more apps (a mini AI chat).                                                                            | Future | Web components and a Hugo shortcode.                                                               |
| R9  | **Modular setup** for endpoints and jobs.                                                                                                                                                           | MVP    | A module registry from the start, so future features are new modules.                             |
| R10 | **Long-running work** must not block the API.                                                                                                                                                       | MVP    | Queue pattern: `202 Accepted` + `job_id`, the client polls the job.                                |

Requirements carried over from the pipeline page still apply: raw captures are never changed, pages are overwritten by stable ID, Python handles files and frontmatter while the LLM only returns JSON, and tags come from a fixed list.

---

## ✅ Decisions {#decisions}

| Topic               | Decision                                                                                                                                                                                  |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Build order         | **MVP first**, and it only processes idea-bucket docs. Everything else is a future feature.                                                                                               |
| MVP scope           | Scheduler + queue + worker + metrics in Postgres + a **minimal API** (start a run, list runs and items). **No UI** in the MVP.                                                             |
| Trigger model       | The API and the scheduler both **enqueue jobs**. The worker does the work.                                                                                                                |
| LLM step            | Its own job type (`llm.reason`). **Provider and model come from the message**, as a named **profile**, with a default per document class (`note` → `notes`, `ai-chat` and `web-clip` → `clippings`, `youtube` and `youtube-gemini` → `youtube`). We start with these three and test them. |
| MVP LLM backends    | **FreeLLMApi** (`notes`) and the **OpenAI API** (`clippings`, `youtube`). Every call is an API call with a key: no CLI, no subscription. Anthropic and Gemini APIs can be added later as another profile. |
| Folders             | **Five top-level folders, one rule:** a run **only looks at `inbox/`** for work. When work on a document starts, it leaves `inbox/`: it gets a **calculated file name** (`YYYYMMDD-<short guid>-<title>.md`, at most 128 characters), the original goes to `archive/` (unchanged except for two added frontmatter lines, `original_filename` and `calculated_filename`) and a working copy to `output/` (with a `stage` in its frontmatter), so a document is never started twice. `output/` then holds the final page. `failed/` holds permanent failures (with an `.error.txt`) and `duplicates/` earlier snapshots of a longer clip. A temporary error stalls the working copy in `output/` (`stage: deferred`, with the reason). **To retry, use `--retry-deferred` (all stalled documents) or `--requeue NAME` (one), or move the file from `archive/` back into `inbox/` by hand**: it keeps its calculated name, so the next run overwrites the stalled copy. The same name is used in `archive/`, `output/` and `epiaku-docs`, so two documents called `New chat.md` never overwrite each other. A run never reads `output/`. Each folder has `notes/` and `clippings/`, and a file keeps its inbox name. See [the layout](../idea-catcher-pipeline/#repo-layout). |
| Failures            | **No fallback between providers.** A failed LLM job is logged and **retried on the next run** (after `retry_delay`). After **3 failed days** the note is flagged **stuck** in the log and metrics, and keeps retrying. |
| Same video twice    | A video clipped directly **and** summarized in Gemini keeps **both** pages (IDs `<video-id>` and `<video-id>-gemini`), so they can be compared. The two pages do **not** link to each other: the cross-link was built and then removed because nobody needed it. The direct page is the default, the Gemini page the fallback and a second opinion ([comparison](../idea-catcher-youtube-methods-comparison/)).                                           |
| One call per class  | Every class (including both YouTube classes) makes exactly **one** LLM call, on its own prompt file. No separate review step and no second call for any class. |
| Queue               | The Postgres `jobs` table **is** the queue, used through a shared module (`enqueue` / `claim` / `complete`) in the API and the worker. **No dedicated queue container** ([why](#queue-placement)). |
| Database            | **PostgreSQL 17** (image `pgvector/pgvector:pg17`) for the queue, logging, metrics and future data. See [Database options](#db-options) for the alternatives.                          |
| One truth (2026-10-04) | **Postgres is the only truth.** There is no mode that works with or without it: when the database is not running, the system does not run (only the single-document tools `scan` and `reason` work without it: they touch no gate, no lock and no checkout, and `reason` refuses YouTube clips). The lock and the gate are per database: two `DATABASE_URL`s on one set of checkouts are two writers and two gates. The YouTube gate and the one-at-a-time lock live only in Postgres, for the worker and for the CLI (B4b); the rest of the file-held state (the LLM blocks, the item states) moves in B5. |
| Hosting             | **One Proxmox LXC with Docker Compose.** The same `compose.yaml` runs locally. No VM.                                                                                                     |
| Service deploy      | **Manual**, with a `deploy.sh` script in the service repo.                                                                                                                                |
| Docs site deploy    | **Unchanged.** The service commits pages to `epiaku-docs` `main`. You deploy the site manually with `deploy.sh`, as today.                                                               |
| Hugo build check    | **Not in the MVP.** Python validates the frontmatter and page structure. Your manual Hugo build catches anything else.                                                                   |
| Repo                | A new **`idea-catcher`** repo for the service (and later its React front end).                                                                                                          |
| Retry               | A deferred LLM job is retried after **`retry_delay`** (default 60 minutes). If the API key's **budget is used up**, it waits **`budget_retry_delay`** (default 6 hours) instead. There is no evening window: every profile is an API and can run at any hour. |
| Retry inside a call  | A transient LLM failure (a 5xx, a timeout, a dropped connection) is tried again **inside the same run**, up to `LLM_MAX_ATTEMPTS` calls (default 5) with a doubling wait. A used-up budget, a 429, a bad key and other 4xx errors are **not** retried. This sits below the job-level `retry_delay` above: after the last attempt the document defers as before. Added in Stage A because the free provider fails often and a retry is routed to another provider. |
| Re-running a document | `run pipeline --requeue NAME` moves the archived original back to `inbox/`, deletes what the earlier run left (`output/`, and for a failed document `failed/` and its `.error.txt`), and processes only that document. It reuses a good saved LLM reply (`--refresh-llm` calls the model again). The saved YouTube facts in `facts/` are kept, so a requeue never calls YouTube again. A document is in **one place** at a time. |
| Tags               | The **idea-type tag is optional, at most one**; topic tags are 1 to 4 and chosen only from the fixed list. A missing tag **never fails a page** (most pages on the site have no tags). The rule "exactly one" was in the first design and failed a good page, so it was dropped. |
| What the LLM may write | **Python owns the facts; the LLM writes prose.** Metrics, upload date, chapters and ids come from code. The one exception is a `youtube-gemini` page, which shows the metrics and chapters Gemini wrote. Links are picked by the LLM and kept only if the URL is literally in the video description. |
| Business context   | A short `epiaku-context.md` tells the **direct** YouTube prompt who Epiaku is, for the Channel Application part. It is **not** sent to the Gemini prompt, which only restructures Gemini's own answer. |
| Dictated notes     | A `glossary.yaml` of often-misheard words is added to the note prompt, and Dutch (or any non-English) notes are translated to English first. The original language is kept in the page frontmatter (`language`). |
| Free model          | FreeLLMApi starts with **`auto`** routing. The model is an **env var** (`FREELLMAPI_MODEL`), so a specific model or a group/chain of models can be set later without code changes.     |
| Access              | LAN/VPN only + **scoped API keys on every endpoint** (read and write).                                                                                                                    |
| Front end (future)  | React apps as **web components** placed in Docsy pages with a Hugo shortcode.                                                                                                             |
| Dashboards (future) | **Built in React**, so we can make simple custom dashboards. No Grafana.                                                                                                                  |
| GitHub Action       | The two-stage rocket's GitHub Action is **no longer needed**, and so is the Claude Code CLI. |

---

## 🔭 MVP vs. Future at a Glance {#glance}

| Area            | MVP                                                                                        | Future                                                                                                   |
| --------------- | ------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| Input           | idea-bucket `inbox/`: notes, AI chats, YouTube clips                                       | YouTube URLs from the API, other sources, web clips                                                      |
| Containers      | `db`, `api`, `worker`                                                                      | + `llm-worker` (split out), + `caddy` for React bundles                                                  |
| Queue           | Postgres jobs table, one job at a time, `run_after` for delays                             | Per-backend parallel slots, parent/child job trees, cancel                                               |
| LLM backends    | `freellmapi`, `openai`, `fake` (tests)                                                     | `anthropic-api`, `gemini-api` (videos without captions), budgets                                        |
| Profiles        | In a config file (`profiles.yaml`)                                                         | Stored in the DB, editable from the dashboard                                                            |
| Schedules       | In `.env` (cron expressions)                                                               | Stored in the DB, editable from the dashboard                                                            |
| API             | Health, start a run, list runs and items. Polling for progress.                           | Metrics, replay, cancel, YouTube, profiles, schedules, keys. Live progress stream.                      |
| API keys        | Listed in `.env` with scopes                                                               | Stored hashed in the DB, managed from the API                                                            |
| Metrics         | Stored: runs, items, events, tokens                                                        | Aggregation endpoints + React dashboards                                                                 |
| YouTube         | Facts (counts, transcript) for direct **clips** only, then the LLM step (`youtube-gemini` makes no YouTube API call at all) | Endpoint, cache, extra verify checks, Gemini for videos without captions                                |
| Long inputs     | Sent whole (the OpenAI models have a 1M context)                                          | Map-reduce chunking, needed for free models on long texts                                                |
| Front end       | None (Swagger UI at `/docs`, `curl`)                                                       | React web components in the docs site                                                                    |
| Fact-checking   | Free, non-LLM checks on both YouTube classes (timestamp and tool checks against the transcript) | An agentic checker with tools (for example open the video page or search) for any class                  |
| Safety          | Python page validation                                                                     | Hugo build check, GitHub App instead of a PAT                                                            |

---

## 🟢 Part 1: MVP {#mvp}

The MVP processes the docs from `idea-bucket` and nothing else. It is deliberately small: three containers, one worker that runs one job at a time, configuration in files, and no UI. It still has the **queue, the LLM profiles and the metrics tables**, because those are hard to add later and everything in Part 2 builds on them.

### 🗺️ Architecture {#mvp-architecture}

```text
  curl · scripts · Swagger UI (LAN/VPN, Authorization: Bearer <key>)
                   │
                   ▼ :8000
 ┌─── LXC "idea-catcher" · Docker Compose (same file on the Mac) ─────────────┐
 │                                                                            │
 │ api (FastAPI) — checks key scope, enqueues jobs, reads jobs/items          │
 │         │ INSERT job (queue, run_after, llm options) + NOTIFY              │
 │         ▼                                                                  │
 │    ┌──────────────────────────────┐                                        │
 │    │ db · PostgreSQL              │                                        │
 │    │ jobs = the queue             │                                        │
 │    │ job_items (metrics per doc)  │                                        │
 │    │ job_events (log)             │                                        │
 │    └──────────────────────────────┘                                        │
 │         ▲ claim: run_after ≤ now, SKIP LOCKED, one job at a time           │
 │         │                                                                  │
 │    ┌────────────────────────────────────────────────────┐                  │
 │    │ worker                                             │                  │
 │    │ scheduler loop ──▶ enqueues pipeline.run           │                  │
 │    │ default queue: pipeline.run · pipeline.publish     │                  │
 │    │ llm queue:     llm.reason (profile → backend)      │                  │
 │    │ failure → deferred to next day · 3 days → stuck    │                  │
 │    └────────────────────────────────────────────────────┘                  │
 │                                                                            │
 └────────────────────────────────────────────────────────────────────────────┘
      │ git pull/push (PAT)             │ LAN                │ HTTPS
      ▼                                 ▼                    ▼
  GitHub: idea-bucket              FreeLLMApi LXC       OpenAI API
          epiaku-docs (main)       (free models)        (HTTPS, API key)
      │
      ▼  manual, as today: ./.github/workflows/deploy.sh → web server 10.10.60.7
```

### 🧩 Components {#mvp-components}

| Component   | Runs as                                         | Responsibility                                                                                                                              | Secrets it holds                                      |
| ----------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| **api**     | Container, `catcher api` (FastAPI, port 8000)   | Checks the API key scope, starts runs (enqueues a job), reads jobs and items. **No Git, no LLM calls, no long work.**                        | API keys                                              |
| **worker**  | Container, `catcher worker`                     | Runs the scheduler loop and claims jobs from **both queues** (`default` and `llm`), **one job at a time**: Git, reading the inbox, YouTube facts, LLM calls, rendering, publishing. | GitHub token, `OPENAI_API_KEY`, FreeLLMApi URL |
| **db**      | Container, `pgvector/pgvector:pg17`, named volume | Jobs (the queue), items, events.                                                                                                            | –                                                     |
| **CLI**     | Same image, `catcher …`                         | Local runs and fixes without the API: `catcher run pipeline --dry-run`, `catcher items requeue <doc_id> --profile …`, `catcher db upgrade`. | –                                                     |

**Why a separate API and worker already in the MVP?** The API must answer while a run takes minutes, and a crash in Git or `yt-dlp` must not take the API down. It is the same codebase and the same image, just two commands.

**Why one worker for both queues?** It is simpler, and the volume is small (a few notes a day, a few chats a week). Because LLM jobs already sit on their own `llm` queue, splitting them out to a separate `llm-worker` container later is a Compose change, not a code change (see [Scaling out](#f-scaling)).

### 📬 Jobs & Queue {#mvp-jobs}

Every job, whoever creates it, is a row in the `jobs` table with the same shape:

```json
{
  "type": "pipeline.run",
  "queue": "default",
  "params": { "dry_run": false },
  "llm": { "profile": "clippings" },
  "trigger": "api",
  "run_after": "2026-09-27T12:00:00+02:00"
}
```

The `llm` block is optional. It is passed down to the `llm.reason` job the run creates for each document; without it, the document's class picks the profile.

```text
 queued ──claim (run_after ≤ now)──▶ running ──▶ succeeded
    ▲                                   │  ├───▶ succeeded_with_warnings
    │                                   │  └───▶ failed
    └──── deferred (run_after = now + retry_delay) ◀── LLM failure, quota or rate limit
```

- **Enqueue:** insert a row with `status='queued'` and `run_after` (now, or later for a deferred job). The API returns `202 Accepted` + `{ "job_id": … }`.
- **Claim:** the worker runs `SELECT … WHERE status='queued' AND run_after <= now() ORDER BY priority DESC, run_after, created_at FOR UPDATE SKIP LOCKED LIMIT 1`. `SKIP LOCKED` keeps this correct when more workers are added later.
- **Wake-up:** the API sends `NOTIFY jobs` after inserting, and the worker `LISTEN`s, so an API-triggered run starts within a second. A 30-second poll picks up delayed jobs whose time has come.
- **One pipeline run at a time.** A second trigger while a `pipeline.run` is queued or running returns the **existing** `job_id`. This is enforced with a partial unique index on `(type) WHERE type = 'pipeline.run' AND status IN ('queued','running')` (only for the run, not for every job type), and an insert uses `ON CONFLICT DO NOTHING` followed by a select, so two simultaneous triggers cannot both win. The CLI's `run pipeline` takes the worker's Postgres lock (B4b), so it never runs next to a worker. Note that a second request is answered with the existing run, whatever its options: a real run asked for while a dry run is queued must not be silently satisfied by it, so the dedupe key has to include the run's parameters (decide in B2).
- **Heartbeat:** a running job updates `heartbeat_at` every ~15 s. On start, the worker resets jobs with an old heartbeat (it crashed mid-job) to `queued`. Every step is idempotent (overwrite by ID), so re-running is safe.
- **Progress:** handlers write `job_events` (level, message) and update `progress_done/total`. Clients **poll** `GET /jobs/{id}`. A live stream is a [future feature](#f-frontend).

### 🧠 LLM Step & Profiles {#mvp-llm}

All reasoning goes through **one function** and **one job type**. A module never calls an LLM client directly.

```python
# catcher/modules/llm/service.py (Stage A, as built: synchronous)
class LlmRequest(BaseModel):
    task: str  # prompt template: "note", "ai-chat", "youtube-summary"
    input: dict  # template variables (note body, transcript, facts, …)
    schema_name: str  # Pydantic output model, e.g. "NoteSummary"
    profile: str  # a named profile from profiles.yaml: "notes", "clippings", "youtube"


def reason(req: LlmRequest, *, profiles: ProfilesConfig, backends: BackendFactory) -> LlmResult:
    """Build the prompt, call the backend, validate the JSON, return the result + usage.
    LlmResult carries the prompt version, backend, model, usage and attempts."""
```

`reason()` is **synchronous** (the backend, `yt-dlp` and Git are blocking too), so the Stage B worker is synchronous as well, with a thread for the heartbeat and one for the scheduler. A per-request `backend` or `model` override does not exist: the profile decides both.

The same function handles notes, AI chats and YouTube clips now, and the YouTube endpoint, web clips or anything else later. A new task means a new prompt template and a new output schema, not new LLM code.

**Saved replies (Stage A, as built).** Every call leaves a trace in `llm/<subfolder>/<calculated name>.json` in `idea-bucket` (the raw reply of each attempt, tokens, model, backend, profile, prompt version, `content_key`, outcome, validated output), and a run **reads a good saved reply before it calls the model**, like it reads saved facts before calling YouTube. A reply is reused only when the task, the profile, the prompt version and the `content_key` (task + document text + transcript) match and the trace ended `ok`. `--refresh-llm` or `LLM_CACHE=false` skips it. Details: [Saved LLM replies](../idea-catcher-how-to-run/#saved-llm-replies).

**Where the profile comes from** (the first one that sets it wins):

1. The `llm` block in the **job message** (for example `POST /pipeline/runs {"llm": {"profile": "notes"}}`).
2. The **default profile of the document class** (`doctypes.py`).
3. The **global default** in `profiles.yaml`.

**MVP profiles** (`profiles.yaml`). Three profiles, one per kind of capture, each an API with a key. We start with these and adjust after testing them with the [model test suite](../idea-catcher-pipeline/#test-suite).

| Profile     | Backend      | Model                                        | Default for                                                       |
| ----------- | ------------ | -------------------------------------------- | ----------------------------------------------------------------- |
| `notes`     | `freellmapi` | `$FREELLMAPI_MODEL` (starts as `auto`)       | **Notes** (`note`)                                                |
| `clippings` | `openai`     | `$OPENAI_MODEL_CLIPPINGS` (e.g. `gpt-6-sol`) | **AI chats** (`ai-chat`: Gemini and Claude clips that are not YouTube) and **web clips** (`web-clip`: any other clipped page) |
| `youtube`   | `openai`     | `$OPENAI_MODEL_YOUTUBE` (e.g. `gpt-6-sol`)   | **`youtube` and `youtube-gemini`** (the latter makes no YouTube API call — it only reformats Gemini's own answer) |
| `fake`      | `fake`       | –                                            | Tests and local development                                       |

```yaml
# profiles.yaml
default: notes
retry_delay: 60m                 # a deferred job is tried again after this (Stage B)
budget_retry_delay: 6h           # same, when the API key's budget is used up (Stage B)
stuck_after_days: 3
profiles:
  notes:     { backend: freellmapi, model: "${FREELLMAPI_MODEL:-auto}" }
  clippings: { backend: openai,     model: "${OPENAI_MODEL_CLIPPINGS}" }
  youtube:   { backend: openai,     model: "${OPENAI_MODEL_YOUTUBE}" }
  fake:      { backend: fake }
```

**Changing the provider of one kind of capture** is a one-line change in `profiles.yaml`: point `youtube` at another `backend` and `model`. `clippings` and `youtube` are separate profiles on purpose, so they can use different providers or models, for example a cheaper model for chats and a stronger one for video summaries. There is no `when` option: every profile is an API and can run at any hour.

**When an LLM job fails, there is no fallback to another provider:**

| What happens                                            | What the worker does                                                                                                                         |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Invalid JSON or schema error                            | Retry **once, immediately**, on the **same** profile, with the validation error added to the prompt. If it fails again, treat it as a failure (below). |
| Timeout, connection error, server error (after the in-call retries) | Log a **warning** event with the reason. The job becomes `deferred` with `run_after = now + retry_delay`.                                   |
| **The document itself is the problem:** empty, longer than `LLM_MAX_INPUT_CHARS`, or a model that answers "context length exceeded" | **Failed for good** without a retry (`InputRejected`): it moves to `failed/` with an `.error.txt`. A retry would fail the same way and cost the same. |
| Rate limit (`429`, `rate_limit_exceeded`)               | Temporary. `deferred` with `run_after = now + retry_delay`. The worker **stops claiming other jobs of the same backend** until then (they would hit the limit too); other backends carry on. |
| **Budget reached** (`insufficient_quota`, billing or spend limit) | The API key's budget is used up, so the backend cannot answer **until the budget is raised or renewed**. The job is `deferred` with `run_after = now + budget_retry_delay` (default 6 hours, so we notice a raised budget the same day without hammering the API). The backend is **blocked for the rest of the run**, and the log gets **one `ERROR` per backend** with the number of waiting notes: `openai budget reached: 4 note(s) waiting; raise the key's budget or point the profile at another provider`. Nothing is lost: the notes stay in `output/` and go through as soon as calls work again. |
| Failing on **3 days** (`stuck_after_days`)              | The note's item is flagged **`stuck`**, with a warning event. It **keeps retrying**. `GET /items?status=stuck` lists these notes, so you can pick another profile with `catcher items requeue <doc_id> --profile …`. |

The working copy stays in `output/` (`stage: deferred`, with the reason) and the untouched original is in `archive/`, so nothing is lost. In Stage A you retry everything a temporary error stalled with `catcher run pipeline --retry-deferred`, or one document with `--requeue NAME`, which moves the original from `archive/` back into `inbox/` (clearing the stale `output/` copy) and runs it again (or by moving the file back by hand).

**MVP backends:**

| Backend      | Client                                                                       | Output handling                                                                                   | Metrics                                        |
| ------------ | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `freellmapi` | OpenAI SDK, `base_url=$FREELLMAPI_URL` (`http://<h4-ip>:3001/v1`)            | Prompted JSON, validated with Pydantic                                                            | Tokens if FreeLLMApi reports them, cost 0      |
| `openai`     | OpenAI SDK, `base_url=$OPENAI_BASE_URL`, `api_key=$OPENAI_API_KEY`           | JSON mode (`response_format={"type": "json_object"}`), validated with Pydantic, one retry | Tokens from the response, cost from the price table |
| `fake`       | Canned JSON per schema                                                       | –                                                                                                 | –                                              |

Both real backends are **one OpenAI-compatible client** with a different base URL, key and model. The API key is read from the environment, is never logged and never written to a page or a sidecar. Set a **monthly spend limit** on the OpenAI key.

### 🧭 One class, one prompt, one call {#mvp-one-prompt-per-class}

Every document class works the same way: one detection rule picks the class, the class names its own prompt file and its own profile, and `reason()` makes exactly **one** LLM call. There is no separate "reviewer" step, no second call for any class, and no `review` job or attribute.

| Class            | Detected from                                    | Prompt              | Provider/model (profile) | LLM calls |
| ---------------- | ------------------------------------------------- | -------------------- | ------------------------- | --------- |
| `note`           | no `source`, or an unrecognised one                | `note.md`             | `notes` (FreeLLMApi)       | 1         |
| `ai-chat`        | Gemini/Claude chat, not about a video              | `ai-chat.md`          | `clippings` (OpenAI)       | 1         |
| `web-clip`       | any other clipped web page                         | `web-clip.md`         | `clippings` (OpenAI)       | 1         |
| `youtube`        | a direct YouTube clip                              | `youtube.md`, given Python's transcript, title, counts | `youtube` (OpenAI) | 1         |
| `youtube-gemini` | a Gemini chat that summarized a video              | `youtube-gemini.md`, given only Gemini's own answer — no facts, no transcript, no YouTube API call | `youtube` (OpenAI) | 1         |

This replaces an earlier design where `youtube` got a second, separate "reviewer" LLM call that re-checked a summary already built from the real transcript — extra cost with no new source of truth. `youtube-gemini` was originally designed to check Gemini's answer against our own transcript, which meant it needed the same YouTube API calls `youtube` needs and could get stuck with nothing published when that endpoint was blocked. It is now treated purely as a clipping-reformatting task: Gemini already produced a good, human-made summary, so `youtube-gemini` takes that summary as its only source, restructures it into the page schema, and never calls the YouTube API — not `yt-dlp`, not the transcript endpoint, not even for the video's own counts.

**Free checks apply only to `youtube`.** Python checks that class's finished summary against the facts it was given — a timestamp later than the end of the video, or a tool named that isn't in the transcript, title or description — and reports them as warnings in the page's frontmatter. Nothing is "fixed" by a second model call; the warning just tells you where to look. Views, likes, subscribers and dates always come from `yt-dlp`, never from the model. `youtube-gemini` has no facts to check against, so it runs no free checks and its page has no metrics table.

**Two YouTube classes:**

| Class            | Comes from                                                                                              | Steps                                                                                                                                                                                             |
| ---------------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `youtube`        | A YouTube link clipped in Obsidian                                                                      | Ingest fetches the facts + transcript → `llm.reason` (`youtube.md`) writes the summary → publish                                                                                                  |
| `youtube-gemini` | A **Gemini web chat** that ran the YouTube summary prompt, clipped in Obsidian (`source` is gemini.google.com and the **first user message contains a YouTube URL**, or an explicit `type: youtube-gemini`) | Ingest parses the video ID straight out of the YouTube URL in Gemini's own chat text (no network call) → `llm.reason` (`youtube-gemini.md`) reformats Gemini's final answer into the page format → publish. No YouTube API call anywhere in this path. |

Both classes write to `idea-bucket/youtube/`, and **both pages are kept** when the same video arrives both ways. The page ID is the video ID for `youtube` and `<video-id>-gemini` for `youtube-gemini`. This rarely happens, and when it does the two summaries can be compared side by side.

### 🔄 Processing Flow {#mvp-flow}

**This is the Stage B target design.** Stage A does the same work in one process, one note after the other: the scan is read-only; the YouTube facts are fetched **inside** the note's processing, after its work has started (the only check before is a read-only look at the gate, so a clip that must wait stays in `inbox/`); and the page is rendered, validated and written as soon as the LLM answers. `pipeline.publish` in Stage A is the commit and push at the end of the run. In Stage B the parts below become separate jobs.

A run is up to three kinds of jobs: one `pipeline.run`, one `llm.reason` per note (exactly one LLM call per document, whatever its class), and a `pipeline.publish` that collects all notes whose result is ready.

```text
 schedule / POST /pipeline/runs
        │
        ▼
 pipeline.run  (default queue)
   1. pull idea-bucket
   2. scan inbox/ (read-only): per document work out class and id in memory
      • unreadable file → archive + failed/<sub>/ + .error.txt
      • earlier snapshots of a longer clip (same id) → archive + duplicates/<sub>/
      • youtube → yt-dlp counts + transcript (with fetch date)
      • youtube-gemini → extract the video URL from Gemini's own chat text only
        (no yt-dlp, no transcript call, no YouTube API of any kind)
      • no transcript (youtube only) → defer (FactsUnavailable), never call the LLM
        on title + description alone
      • files that are not markdown (artifacts) → renamed YYYYMMDD-<guid>-<original name>,
        copied to archive/artifacts/ and epiaku-docs idea-bucket/artifacts/ (no LLM job)
   3. per remaining document (respecting --limit / --file) right before its LLM step:
      calculated name YYYYMMDD-<short guid>-<title>.md (kept if it already has one),
      original → archive/<sub>/ (+ 2 frontmatter lines), working copy (stage: analyzed)
      → output/<sub>/, remove it from inbox/ (a stalled copy is overwritten)
   4. per document without an open LLM job → enqueue llm.reason
      with the run's llm options or the class default profile
        │
        ▼
 llm.reason  (llm queue, run_after = now)
   5. reason(): the class's own prompt template → backend → validated JSON
      (youtube: transcript + facts in; youtube-gemini: only Gemini's answer
      in, reformatted, no facts of any kind)
   6. store result + usage on the note's job_item
   7. enqueue pipeline.publish (folded: at most one queued)
        │                         failure → deferred to the next day
        ▼
 pipeline.publish  (default queue)
   8. pull both repos
   9. per ready item: keep allowed tags, render the Hugo page (Jinja),
      validate frontmatter, overwrite the page by id,
      done: the working copy output/<sub>/<name>.md becomes the final page (drops
      `stage`), with the YouTube facts next to it;
      a failed validation moves it to failed/<sub>/ + .error.txt
  10. commit + push epiaku-docs (main), then idea-bucket
        │
        ▼
 you: ./.github/workflows/deploy.sh (manual, as today)
```

- **Overwrite by ID:** delete any existing page in the target folder whose frontmatter `id` matches, then write the page under its **calculated file name** `YYYYMMDD-<short guid>-<title>.md`, the same name as in `archive/` and `output/`.
- **One Git writer:** only the worker touches the repos, and it runs one job at a time. `catcher run pipeline` (also `--dry-run`) and `catcher render` take the same Postgres advisory lock as the worker, so one run or worker works at a time (B4b; before that a `pipeline.lock` file guarded only the CLI). The phone may still push to idea-bucket during a run, so use `pull --rebase` before pushing and retry once.
- **Per-note errors don't fail the run.** A bad note is marked `failed` (moved to `failed/`) or `deferred` (stalls in `output/` with `stage: deferred`), and the other notes go on.
- **Python validation instead of a Hugo build:** the rendered page must parse as YAML frontmatter + markdown, contain `title`, `description`, `weight` and `type: docs`, only use tags from the allowed list, and only use known shortcodes (`youtube-lite`).
- **Unknown tags** from the LLM are dropped from the page and logged as a `tag_suggestion` event.

The document-type registry, the prompt templates, the output schemas and the Jinja page templates are described in the [Implementation Reference](../idea-catcher-pipeline/#implementation) on the pipeline page.

### ⏰ Scheduling {#mvp-scheduling}

The scheduler runs as a loop inside the worker and **only enqueues jobs**. In the MVP the schedule lives in `.env`:

```bash
SCHEDULE_PIPELINE_RUN="0 8,12,17,21 * * *"   # local time, cron syntax (croniter): process the inbox
SCHEDULE_IDEAS_PULL="*/30 * * * *"           # pull idea-bucket often: new captures from Obsidian
SCHEDULE_PUBLISH="0 6,12,18,23 * * *"        # commit + push the results, less often than the pull
FREELLMAPI_URL="http://<h4-ip>:3001/v1"
FREELLMAPI_MODEL="auto"                      # later: a specific model or a model group/chain
```

Every 30 s the loop checks whether a schedule is due, and enqueues the matching job: a `pipeline.run` (the dedupe rule applies), a pull of `idea-bucket`, or a `pipeline.publish` (commit and push). Pulling, processing and publishing have **separate schedules**, so each can be tuned on its own. The captures are backed up on GitHub from the moment Obsidian pushes them, so only results that were not yet pushed (and the saved YouTube facts) would be lost in a disk crash; see [When to commit](../idea-catcher-youtube-bans-and-queue-options/#when-to-commit). After downtime it runs a missed schedule **once**, not once per missed slot. *As built in B6 (2026-10-07):* see "Built: B6" under Stage B and [Scheduling](../idea-catcher-how-to-run-stage-b/#scheduling). Every capture is published within one run, because every LLM profile is an API that can be called at any hour.

### 🗄️ Database & Metrics {#mvp-database}

**PostgreSQL 17**, with migrations in **Alembic** (why Postgres: see [Database options](#db-options)). The tables as built (B5, migration `0005`; since B6 the scheduler uses `schedules`: one row per schedule name with its `last_fired_at`):

```text
jobs        id (uuid), type, status, priority, run_after, reason, params jsonb, result jsonb, error,
            attempts, max_attempts, locked_by, lease_until, heartbeat_at, dedupe_key, resource,
            claim_seq, created_at, started_at, finished_at
job_items   id, calculated_name (unique), doc_id, doc_class, origin, root_job_id → jobs (the pipeline.run
            that staged it), status (staging / waiting_youtube / waiting_llm / ready / published /
            deferred / stuck / failed / duplicate), stage_reason, stage_since,
            inbox_path, archive_path, output_path, failed_path, docs_page, original_filename,
            llm_profile, llm_backend, llm_model, prompt_version, tokens_in, tokens_out,
            llm_duration_ms, llm_result jsonb {"attempts", "saved"}, warnings jsonb (dropped tags),
            error, created_at, updated_at
job_events  id, job_id, item_id, ts, level (info / warning / error), message, data jsonb
resources   name (youtube, openai, freellmapi, openai:clippings...), next_allowed_at, blocked_until,
            blocked_at, streak, concurrency, reason, updated_at
```

This already answers the metrics questions, even before a dashboard exists. The queries below were run on a throwaway database on 2026-10-06. **Read the LLM columns only for `published` rows**: a row holds its last run, and a reply served from a saved one (`llm_result.saved`) cost nothing now, so the cost query leaves it out.

| Question                                       | Query                                                                                                                                                                                                         |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| When did each run happen, and how did it end?  | `SELECT id, status, created_at, finished_at, result FROM jobs WHERE type = 'pipeline.run' ORDER BY created_at DESC`                                                                                         |
| How many docs of each class per day?           | `SELECT date(created_at) AS day, doc_class, status, count(*) FROM job_items GROUP BY 1, 2, 3 ORDER BY 1, 2, 3`                                                                                                |
| ... per run?                                   | `SELECT root_job_id, status, count(*) FROM job_items GROUP BY 1, 2`                                                                                                                                          |
| Warnings and errors?                           | `SELECT e.ts, e.level, i.calculated_name, e.message FROM job_events e LEFT JOIN job_items i ON i.id = e.item_id WHERE e.level IN ('warning', 'error') ORDER BY e.ts DESC`                                      |
| Which model, how many tokens (what we paid)?   | `SELECT llm_backend, llm_model, count(*), sum(tokens_in), sum(tokens_out) FROM job_items WHERE status = 'published' AND (llm_result->>'saved')::bool IS NOT TRUE GROUP BY 1, 2`                               |
| What is waiting or stuck?                      | `SELECT calculated_name, status, stage_since, stage_reason FROM job_items WHERE status IN ('waiting_youtube', 'waiting_llm', 'deferred', 'stuck') ORDER BY stage_since` (or `catcher items list --status stuck`) |
| Which pages dropped tags?                      | `SELECT calculated_name, warnings FROM job_items WHERE warnings IS NOT NULL`                                                                                                                                 |
| Which backend or profile is blocked, and why?  | `SELECT name, blocked_until, reason FROM resources WHERE blocked_until IS NOT NULL`                                                                                                                         |

Backups: a nightly `pg_dump` to a mounted folder, plus the Proxmox backup (vzdump) of the LXC.

### 🔌 API {#mvp-api}

A minimal API under `/api/v1`. FastAPI generates the OpenAPI schema, and the **Swagger UI at `/docs`** is the MVP's user interface.

| Method & path                    | Scope  | Purpose                                                                                     | Response                           |
| -------------------------------- | ------ | ------------------------------------------------------------------------------------------- | ---------------------------------- |
| `GET /health`                    | none   | Liveness, and whether the worker's heartbeat is recent                                      | `200` / `503`                      |
| `POST /api/v1/pipeline/runs`     | `run`  | Start a run: `{ dry_run?, llm? }`                                                           | `202 {job_id}`, or `200` + the existing `job_id` if one is already queued or running |
| `GET /api/v1/jobs?type=&status=&from=&to=` | `read` | List jobs (runs, LLM jobs, publishes)                                             | Paged list                         |
| `GET /api/v1/jobs/{id}`          | `read` | Status, progress, timings, events, and for a run: item counts per class and status          | Job                                |
| `GET /api/v1/items?doc_class=&status=&from=` | `read` | Processed notes: status, profile, model, tokens, warnings, output path          | Paged list                         |

Callers poll `GET /jobs/{id}` for progress. Metrics aggregation, replay, cancel, profiles and the YouTube endpoint are [future features](#future).

### 🔐 Security {#mvp-security}

| Layer     | MVP setup                                                                                                                                                    |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Network   | The LXC is reachable only on the LAN/VPN. No port forwarding.                                                                                                |
| API keys  | `Authorization: Bearer <key>` on **every** endpoint except `/health`. Keys are listed in `.env` as `name:scopes:key` (for example `mac:read,run:…`). One key per device. |
| Scopes    | `read` (jobs, items), `run` (start runs).                                                                                                                    |
| Secrets   | In `.env` on the LXC (not in Git): GitHub fine-grained PAT (Contents read/write on `idea-bucket` and `epiaku-docs` only), `OPENAI_API_KEY`, DB password, API keys. |

### 🖧 Hosting & Deploy {#mvp-hosting}

**Docker Compose on both the Mac and the LXC.** The requirement is to test the whole solution locally and have it run **the same way** on Proxmox. Compose gives exactly that: the same images, the same `compose.yaml`, the same Postgres version. Only the `.env` file differs (secrets, repo remotes, schedule).

| Where                | How                                                                                                                                             |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| **Mac (test)**       | OrbStack or Docker Desktop. `docker compose up`. `.env.local` points Git at **local bare copies** of the two repos and uses the `fake` or `notes` profile. |
| **Proxmox (prod)**   | An unprivileged Debian LXC with `nesting=1` and `keyctl=1`, Docker installed, the same pattern as the FreeLLMApi LXC. `.env` points at the real GitHub repos. |

```yaml
# compose.yaml
x-app: &app
  build: .
  image: idea-catcher:latest
  restart: unless-stopped
  env_file: .env
  depends_on: { db: { condition: service_healthy } }

services:
  db:
    image: pgvector/pgvector:pg17   # plain Postgres 17 + the pgvector extension (enabled only when needed)
    restart: unless-stopped
    volumes: [pgdata:/var/lib/postgresql/data]
    environment: { POSTGRES_DB: catcher, POSTGRES_USER: catcher, POSTGRES_PASSWORD: "${DB_PASSWORD}" }
    healthcheck: { test: ["CMD", "pg_isready", "-U", "catcher"], interval: 5s }

  api:
    <<: *app
    command: catcher api --host 0.0.0.0 --port 8000
    ports: ["8000:8000"]

  worker:
    <<: *app
    command: catcher worker          # scheduler loop + both queues, one job at a time
    volumes: [repos:/data/repos]     # persistent clones of idea-bucket + epiaku-docs

volumes: { pgdata: {}, repos: {} }
```

The image holds Python 3.12 + uv, Git, `yt-dlp` + Deno. There is **no Node, no Claude Code CLI and no Hugo** in the image, since the docs site deploy stays manual.

**Deploying the service (manual, like the docs site):**

```bash
# idea-catcher/deploy.sh (sketch)
set -euo pipefail
TARGET=root@<idea-catcher-lxc-ip>
make test                                   # layers 1–6 must pass, or nothing is deployed
rsync -az --delete --exclude .env --exclude .git ./ "$TARGET:/opt/idea-catcher/"
ssh "$TARGET" "cd /opt/idea-catcher && docker compose up -d --build && docker compose exec -T api catcher db upgrade"
ssh "$TARGET" "cd /opt/idea-catcher && make smoke"   # health, heartbeat, selftest, dry-run job
```

The image is built **on the LXC**, so no registry is needed. Tests run **before** the rsync and a smoke test runs **after** it (see [What runs when](#test-when)). `.env` lives only on the LXC and is never rsynced.

{{% alert title="Docker in an LXC" color="warning" %}}
Proxmox officially recommends VMs for Docker, and Docker inside an LXC can break after Proxmox or kernel upgrades (overlayfs, AppArmor, cgroups). It already works for FreeLLMApi. Take a snapshot of the LXC before upgrading Proxmox, and check the stack afterwards.
{{% /alert %}}

### 📁 Repo Layout {#mvp-layout}

A new repo, **`idea-catcher`**:

```text
idea-catcher/
├── compose.yaml
├── Dockerfile
├── deploy.sh
├── .env.example                 ← every setting, no secrets
├── profiles.yaml                ← the three LLM profiles (notes, clippings, youtube), retry rules
├── pyproject.toml               ← uv
├── src/catcher/
│   ├── cli.py                   ← catcher api | worker | run … | items … | db upgrade
│   ├── api/                     ← app factory, API key auth + scopes, mounts module routers
│   ├── worker/                  ← claim loop, heartbeat, scheduler loop
│   ├── core/                    ← config, db (SQLAlchemy), queue, registry, git, logging
│   └── modules/
│       ├── llm/                 ← service.py (reason), backends/, prompts/, schemas.py
│       ├── pipeline/            ← doctypes.py, templates/ (Jinja), jobs.py, router.py, tags.yaml, glossary.yaml
│       └── youtube/             ← facts.py (yt-dlp, transcript) — used by pipeline for clips
├── migrations/                  ← Alembic
├── tests/                       ← unit, component, integration, api, e2e, live + fixtures (see Testing)
├── compose.test.yaml            ← test overrides: fake LLM, bare repos, test keys
├── Makefile                     ← make test | test-live | eval | smoke
└── .github/workflows/test.yml   ← default suite on push (no secrets, no deploy)
```

Each module has the same shape (a router, job handlers, schemas), so future features are **new folders under `modules/`**.

### ▶️ How to Run {#mvp-run}

**Locally, the full stack:**

```bash
docker compose --env-file .env.local up --build
```

Swagger UI is at `http://localhost:8000/docs`.

**Quick iteration without Docker or a database** (the [stage A](#mvp-stage-a) way of working, which keeps working later): `--no-db` (**planned for Stage B**, today the CLI is the only mode) calls the pipeline functions directly instead of going through the queue.

```bash
uv run catcher run pipeline --dry-run --no-db --llm-profile fake --ideas ../idea-bucket --docs ../epiaku-docs
```

**Start a run through the API:**

```bash
curl -X POST http://<idea-catcher-lxc-ip>:8000/api/v1/pipeline/runs -H "Authorization: Bearer $CATCHER_KEY"
```

**Check what is waiting or stuck:**

```bash
curl "http://<idea-catcher-lxc-ip>:8000/api/v1/items?status=stuck" -H "Authorization: Bearer $CATCHER_KEY"
```

### 🧪 Testing {#mvp-testing}

One automated test suite in the `idea-catcher` repo, run with **pytest**. It is organized in layers, from fast unit tests to a full end-to-end run. The layers are separated with folders and pytest markers, so every moment (while coding, before a commit, before a deploy) runs the right subset.

**Guiding rules:**

- **No real LLM, network or GitHub in the default suite, and no end-to-end tests.** The `fake` backend, HTTP mocked with `respx`, recorded `yt-dlp` output and local bare Git repos make the tests **fast, free and repeatable**. Anything that calls a real LLM (the `live` marker), and the end-to-end tests (the `e2e` marker), are **run manually** on purpose. `pytest` deselects both by default (`addopts = -m "not live and not e2e"` in `pyproject.toml`), so `uv run pytest` never spends API budget.
- **A real Postgres, not a mock.** The queue depends on `SKIP LOCKED`, `LISTEN/NOTIFY` and the unique indexes, which can't be mocked meaningfully. Tests use the same `pgvector/pgvector:pg17` image as production.
- **A frozen clock.** The retry delay and "stuck after 3 days" are about time. Tests set the time with `time-machine` instead of waiting.
- **Each build stage adds its own layers** (see [Tests per stage](#tests-per-stage)).

#### Test layers {#test-layers}

| Layer | What it tests | Examples | Dependencies | Speed |
| --- | --- | --- | --- | --- |
| **1. Unit** (`tests/unit`) | Pure functions: the "algorithm" | `detect()` class detection (incl. `youtube-gemini`), ID and filename rules, frontmatter cleaning, YouTube URL parsing, allowed-tag filtering, page validation, **profile resolution** (message > class > global), **`run_after` calculation** (retry delay, stuck after 3 days), the free timestamp/tool checks on YouTube summaries | None | Milliseconds |
| **2. Component** (`tests/component`) | One module with its outside world faked | `reason()` with the `fake` backend; the `freellmapi` and `openai` backends with HTTP mocked (`respx`: canned JSON, invalid JSON, `429` rate limit, `insufficient_quota`, timeout); YouTube facts from **recorded `yt-dlp` / transcript JSON**; Jinja rendering compared with **snapshot pages** (`syrupy`) | None | Milliseconds |
| **3. Git** (`tests/integration/git`) | Processing the inbox against **real Git**, with local bare repos in a temp folder | A document leaves `inbox/` when work starts (archive + working copy in `output/`), ends as the final page; failures go to `failed/`, earlier snapshots to `duplicates/`; a deferred document stalls in `output/` with `stage: deferred`; moving the file from `archive/` back into `inbox/` overwrites the stalled copy; `--file` and `--limit` only touch the chosen documents; overwrite by ID; both YouTube pages kept; a "phone push" in the middle of a run is handled by `pull --rebase`; `--push` off pushes nothing | `git` | Seconds |
| **4. DB & queue** (`tests/integration/db`) | Migrations, the queue, the metrics tables against a **real Postgres** | Alembic upgrade from empty (and downgrade); `enqueue`/`claim`/`complete`; **two workers never claim the same job**; `run_after` in the future is not claimed; dedupe of `pipeline.run`; heartbeat reaper re-queues a crashed job; `NOTIFY` wakes the worker; deferral + stuck with a frozen clock; folded `pipeline.publish` | Postgres container | Seconds |
| **5. API** (`tests/api`) | FastAPI routes on the test database | No key → `401`, wrong scope → `403`; `POST /pipeline/runs` → `202`, second call → `200` + same `job_id`; filters on `/jobs` and `/items`; an **OpenAPI schema snapshot** (the contract for the future React client) | Postgres container | Seconds |
| **6. End-to-end** (`tests/e2e`, marker `e2e`, **manual**) | The **whole stack** in Compose, with the fake LLM | `docker compose -f compose.yaml -f compose.test.yaml up`; seed a bare idea-bucket with **one fixture note per class**; `POST /pipeline/runs`; poll the job; assert the pages in the bare epiaku-docs repo, `archive/`, `output/`, `job_items` metrics; run **`hugo` on the result** as the build check the worker doesn't do | Docker, `hugo` on the host | ~1–2 min |
| **7. Live** (`tests/live`, marker `live`, **manual**) | The real outside world, opt-in | One tiny note through the real OpenAI API (`notes` and `clippings` profiles); one note through real FreeLLMApi; `yt-dlp` + transcript on one known video; `git ls-remote` on both GitHub repos | Network, API keys, a few cents of API usage | Minutes |
| **8. Model evals** | **Quality** of prompts and profiles | The [model test suite](../idea-catcher-pipeline/#test-suite): golden notes, chats and videos per class, scored per profile (schema valid, required sections, facts correct) | Real LLMs | Minutes, costs usage |

Layers 1–5 form the **default suite** (`uv run pytest`). They need no secrets and no internet, so they can run anywhere, including GitHub Actions. **Layers 6, 7 and 8 are manual:** the end-to-end run (`uv run pytest -m e2e`), the live tests that call real LLMs and services (`uv run pytest -m live`), and the model evals. They spend API budget or need Docker, so you start them yourself, for example before a deploy. Layer 8 is not pass/fail on exact text; it compares profiles and prompt versions.

#### Test fixtures {#test-fixtures}

```text
tests/
├── unit/  component/  integration/{git,db}/  api/  e2e/  live/
├── conftest.py                    ← Postgres fixture, bare-repo factory, frozen clock, API client with test keys
└── fixtures/
    ├── idea-bucket/inbox/         ← one real-looking note per class: note, ai-chat, web-clip, youtube, youtube-gemini, no-template
    ├── youtube/<video-id>.json    ← recorded yt-dlp metadata + transcript
    ├── llm/<task>/<case>.json     ← recorded LLM outputs: good, invalid JSON, usage limit
    └── pages/                     ← expected Hugo pages (snapshots)
```

- **Recorded, not generated:** fixtures are captured once from real runs (a `catcher fixtures record` helper) and then committed, so tests reflect real formats.
- **Test data from the real repos:** `tests/data/` holds a committed copy of only the folders the Idea Catcher uses (`idea-bucket/inbox/` with the captures, and the empty Hugo `idea-bucket` output folders of `epiaku-docs`). `catcher testdata reset` turns it into fresh git repos without a remote in `tmp/ic`, for manual tries, and the test suite runs the whole pipeline on the same data.
- **Snapshots** of rendered pages make template changes visible in the diff. Update them on purpose with `pytest --snapshot-update`.
- **Postgres for tests:** a session-scoped fixture starts the `pgvector/pgvector:pg17` container (with `testcontainers`, or the `db` service from `compose.test.yaml`). It runs the Alembic migrations once, and gives each test a clean database (truncate between tests).

A sketch of the queue test that matters most:

```python
# tests/integration/db/test_queue.py
async def test_two_workers_never_claim_the_same_job(queue):
    ids = {await queue.enqueue("demo.sleep", {}) for _ in range(20)}

    async def drain():
        got = []
        while job := await queue.claim(queue="default", worker_id="w"):
            got.append(job.id)
            await queue.complete(job.id)
        return got

    a, b = await asyncio.gather(drain(), drain())
    assert set(a) | set(b) == ids and not set(a) & set(b)
```

#### What runs when {#test-when}

| When | What runs | Command | Blocks |
| --- | --- | --- | --- |
| While coding | Unit + component for the module you're working on | `uv run pytest tests/unit -x` (or `ptw` to watch) | – |
| Before a commit | `ruff` (lint + format), `pyright` (types), unit + component | A **pre-commit** hook | The commit |
| On push to GitHub (recommended) | The default suite (layers 1–5) on GitHub Actions, with a Postgres service container. No secrets, no LLM, no e2e, no deploy. | `.github/workflows/test.yml` | A red badge; nothing is deployed from CI |
| **Before a deploy** | The default suite (layers 1–5) and `docker compose build`. You **run the end-to-end tests (layer 6) by hand** first when the change touches the flow. | `make test` inside `deploy.sh`; `uv run pytest -m e2e` by hand | **The deploy.** `deploy.sh` stops if the default suite fails. |
| **After a deploy** | **Smoke test** on the LXC: `/health` is `200`, the worker heartbeat is recent, migrations are at head, and `catcher selftest` passes (below). Then a `POST /pipeline/runs {"dry_run": true}` with a smoke key is polled to success. | The end of `deploy.sh` | Prints a clear failure. Roll back by redeploying the previous commit. |
| Manually: weekly, or before a bigger change | The live tests (layer 7), which call the real LLM APIs | `CATCHER_ALLOW_NETWORK=1 uv run pytest -m live` | – |
| When prompts, profiles or models change | Model evals (layer 8) | `catcher eval --profiles notes,clippings,youtube` | The prompt/profile change |

**`catcher selftest`** runs inside the worker container and checks the real outside world **without spending LLM usage**:

- the database is reachable and migrations are at head;
- `OPENAI_API_KEY` is set and `GET /v1/models` on the OpenAI API answers;
- FreeLLMApi answers `GET /v1/models`;
- the GitHub token can read both repos (`git ls-remote`);
- `yt-dlp --version` works.

It is part of the post-deploy smoke test, and it can be run by hand whenever something looks wrong.

#### Tests per build stage {#tests-per-stage}

| Stage | Test layers added | Done when |
| --- | --- | --- |
| [A: Python pipeline](#mvp-stage-a) | 1 Unit, 2 Component (mocked HTTP, recorded YouTube data, page snapshots), 3 Git, pre-commit. No live or e2e tests in the default run. | Every class has fixtures and snapshot pages, and the Git flow passes on bare repos |
| [B: Postgres + queue](#mvp-stage-b) | 4 DB & queue, frozen-clock tests for deferral and stuck | The concurrency, dedupe, reaper and deferral tests pass |
| [C: API](#mvp-stage-c) | 5 API, 6 End-to-end (manual), the GitHub Actions workflow | `make test` runs layers 1–5 green, locally and in CI, and `uv run pytest -m e2e` passes by hand |
| [Proxmox](#mvp-stage-deploy) | `catcher selftest`, post-deploy smoke, 7 Live, 8 Model evals | `deploy.sh` runs tests before and smoke after, and the evals pick the profiles |

### 🪜 Build Steps {#mvp-steps}

The MVP is built in **three stages**. Each stage is a series of small steps: build a little, test it locally, refactor until we're satisfied, and only then move on. Each stage **wraps** the code of the previous one instead of rewriting it:

```text
 Stage A: plain Python functions + CLI         ← the real work: scan, reason, render, publish
 Stage B: + Postgres queue: jobs call those same functions
 Stage C: + API: enqueues jobs and reads the results from Postgres
 Then:    deploy the same Compose stack to the Proxmox LXC
```

To make that possible, the core logic lives in **plain functions with no knowledge of the queue or the API** (`scan_inbox()`, `reason()`, `render_page()`, `publish()`). The CLI calls them directly in stage A. Job handlers call them in stage B.

#### Stage A: Python pipeline + LLM APIs, run locally {#mvp-stage-a}

**Goal:** process real notes from `idea-bucket` into pages in `epiaku-docs`, run by hand from the Mac. No database, no Docker, no API.

| Step | What we build                                                                                                         | How we test it                                                                                                     |
| ---- | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| A1   | The `idea-catcher` repo, `uv`, the package layout, config from `.env`, logging                                         | `uv run catcher --help`                                                                                            |
| A2   | Read the inbox: scan `inbox/` (read-only), detect the class, derive IDs, find earlier snapshots (`duplicates/`), move unreadable files to `failed/`; then start work per document: original to `archive/`, working copy to `output/`                                  | On a **local copy** of idea-bucket: `catcher scan --ideas ../idea-bucket-copy`, then inspect the output of `catcher scan`          |
| A3   | The `llm` module: `reason()` with the `fake`, `freellmapi` and `openai` backends, `profiles.yaml` (`notes`, `clippings`, `youtube`), output schemas, prompts for notes | `catcher reason <output-note> --profile notes` prints validated JSON. The `openai` profiles need `OPENAI_API_KEY` in `.env`. |
| A4   | Rendering + validation: Jinja page templates, allowed tags, frontmatter checks, overwrite by ID                        | `catcher render <output-note>` writes into a local copy of epiaku-docs. Check it with `hugo server`.               |
| A5   | Publishing: the working copy in `output/` becomes the final page, `failed/` for permanent failures, `stage: deferred` for temporary ones, commit, push, with a `--push` flag that is **off by default**                                    | Without `--push` first (inspect the local commits), then against the real GitHub repos with `--push`               |
| A6   | AI chats, then YouTube clips (`yt-dlp` + transcript), then the `freellmapi` backend                                  | Real clips from the inbox, one class at a time                                                                     |
| A7   | The `youtube-gemini` class: **one call that only restructures Gemini's own answer** (no YouTube call, no fact-check). It keeps Gemini's metrics, chapters and advice | Process a pipeline-made and a Gemini-made summary of the same video (both pages are kept), and compare them with the transcript ([comparison](../idea-catcher-youtube-methods-comparison/)) |
| A8   | One command for the whole flow: `catcher run pipeline [--dry-run] [--push]`                                            | A full run on a copy, then a real run                                                                              |

**Done when:** a real run turns every doc class in the inbox into correct pages on GitHub, and we are happy with the prompts, templates and code structure. Nothing is stored about a document's state in this stage (no Postgres): the folder a file is in, its `stage` and the Python log show what happened, and a deferred document stalls in `output/`. Retry, deferral and metrics come in stage B.

**Status: done (2026-10-01).** Stage A ran for five days (2026-09-27 to 2026-10-01): about 50 commits, about 3,500 lines of Python, 435 tests (after the review fixes of 2026-10-02), five document classes. The full story, with what changed from this plan and the lessons, is in [Stage A: what we built and what we learned](../idea-catcher-stage-a-lessons-learned/). In short, compared with the steps above:

- **Changed:** the `staging/` folder became the inbox-only flow with calculated names; the `claude -p` subscription backend became API-key profiles; the separate YouTube reviewer became free Python checks; `youtube-gemini` stopped checking against YouTube.
- **Added (not in the plan):** the `web-clip` class, artifacts (PDFs and images), `--requeue`, in-call retries, a glossary and Dutch translation, the original language, chapters, links, the upload date, a business context, clear errors for wrong paths, a version string, YouTube protections against an IP ban (saved facts per video, one paced extraction, a gap between fetches, a breaker), and a committed test data set with `testdata reset`.
- **Not done, left for later:** a Hugo build check, the model test suite, long-input chunking and the stuck-note rules (stage B).

#### Stage B: Postgres + the queue, locally {#mvp-stage-b}

**Goal:** the same work, now driven by jobs in a Postgres queue, with deferral, retries, schedules and the state of every document in the database. Still local, without an API.

*Updated 2026-10-01 after Stage A.* The steps below include what we decided since the first plan: our **own** Postgres queue (see [YouTube IP bans and the queue](../idea-catcher-youtube-bans-and-queue-options/)), the YouTube gate moving into the database, separate schedules for pulling and publishing, and a low-priority backfill. Steps **B0, B4, B6 and B8 are new**.

**Stage B decisions (2026-10-02, after a design review against the Stage A code).** These change what the steps below mean; where a step text disagrees, this list wins.

1. **A YouTube clip that has to wait is staged, not left in `inbox/`.** It gets a database row when it is first seen (state `waiting_youtube`, working copy in `output/` with the same `stage`), so it shows up in dashboards and survives a crash. The queue picks the oldest due clip first (`priority DESC, run_after, created_at`); a clip pushed to a later `run_after` does not block the others.
   *Why the claim order is `priority DESC, run_after, created_at` and not plain `created_at`:* the YouTube gate defers a clip by setting its `run_after` to the gate's next slot, so while the gate is closed no clip can take a slot. Clips deferred to the same slot tie on `run_after` and then go oldest-first by `created_at`; a clip that arrives after the slot is open competes only with the clips that are due, so the gate path cannot starve an older clip. If B4 finds otherwise, the `ORDER BY` and its index (`ix_jobs_claim`) must be revisited. *B4 (2026-10-04):* the order stays; the claim now also skips a fetch job while the gate is closed, and when the slot opens the oldest waiting fetch job is claimed first (tested).
2. **The worker and the queue code are synchronous** (`reason()`, `yt-dlp` and Git block anyway), with one thread for the heartbeat and one for the scheduler.
3. **Retries start simple.** The in-call retries stay as they are (`LLM_MAX_ATTEMPTS`). After them an item is `deferred` or `failed` (as in Stage A). There is **no job-level `max_attempts`/`dead`/`retry_delay` layer for LLM errors** at first; we add one only if this proves too little. A closed YouTube gate, a block or a used-up budget wait through `run_after` and do not count as a failure. A **scheduled** run re-queues the deferred items itself (what `--retry-deferred` does by hand). The only attempt counter is for **crashes**: a job whose lease expires counts one attempt and becomes `failed` after 3.
4. **An item is identified by its calculated name** (`<subfolder>/<name>.md`), which is unique. Two captures with one `id` are two items. If both finish, two nearly identical pages may come out (rare, accepted); the page in `epiaku-docs` is still replaced by `id`.
5. **Order of writes: the database row first, then the file move.** The item row (with its calculated name) and its job are committed **before** `start_work` moves anything. After a crash the worker adopts items that have a row but no job result, and `catcher reconcile` rebuilds from the folders and the Git history.
6. **Git:** the commit set is "everything in the managed folders" (`archive/ output/ failed/ duplicates/ facts/`, plus inbox deletions), not a list of touched paths. Push right after each commit; abort a failed rebase and report it. A `git` resource with concurrency 1. A requeue goes through the tool, not by hand. Once a worker exists, the CLI refuses to run against the live remote.
7. **Schedules:** cron strings in `.env`, an explicit timezone, one scheduler, and a small table with `last_fired_at` so a missed slot runs once.
8. **Time:** every queue and gate query takes `now` from Python (never SQL `now()`), so tests can freeze it.
9. **One generic `resource` table** (YouTube, `openai`, `freellmapi`, `git`): `name`, `next_allowed_at`, `blocked_until`, `blocked_at`, `streak`, `concurrency`. A used-up budget is remembered between runs. A missing or damaged row means **closed**. *B4 built the `youtube` row; B5 added the LLM rows (`openai`, `freellmapi`, `<backend>:<profile>`), where a missing row means **open**: an LLM block is only made by a failure.*
10. **Reconcile and the mirror:** Postgres is the truth for processing state. `catcher reconcile` rebuilds item state, name and class from the folders and the frontmatter; it cannot rebuild attempts, tokens, events or the YouTube gate. While the database lives, a missing `youtube` row means **closed** (the gate inserts it closed and logs an ERROR). A freshly migrated database starts **open** (migration `0004` seeds the row open; seeding it closed would make every new install wait `YOUTUBE_BLOCK_HOURS` for nothing), so after a rebuild check the log for a block before you queue clips; B5's `reconcile` closes the gate, because a rebuild may have lost a block. The frontmatter mirror uses **`stage`, `stage_reason`, `stage_since`**; reconcile also reads the Stage A names (`analyzed_at`, `deferred_at`, `deferred_reason`).
11. **Our own queue first.** Procrastinate only if the queue core passes about 300 lines or shows concurrency bugs; switching later changes the tables, and that is accepted.
12. **Dry runs never go through the queue**; the CLI runs them inline. So the "one run at a time" dedupe only ever sees real runs. The dedupe key is part of the job row (`dedupe_key`), not tied to a job type.
13. **Job status** is `queued`, `running`, `succeeded`, `failed`, `cancelled` (text with check constraints). Deferral is `queued` with a future `run_after` and a reason; the word *deferred* belongs to the item. A higher `priority` number goes first.
14. **Database stack:** sync SQLAlchemy 2 with psycopg 3, and Alembic. Tests run against a real Postgres 17 in Docker (never a mock of the queue).

| Step | What we build | How we test it |
| ---- | ------------- | -------------- |
| B0 (built 2026-10-02) | **Make the run steppable.** `run_pipeline` is one loop today. Split it into steps a job can call on its own: scan and stage, process one document, publish what is ready. Behaviour stays the same. Do this **first**: everything else depends on it | The Stage A tests (unit, component, git integration) stay green and unchanged |
| B1 (built 2026-10-02) | `compose.yaml` with only `db` (`pgvector/pgvector:pg17`), SQLAlchemy, Alembic. Tables `jobs`, `job_items`, `job_events`, plus **`resources`** (the YouTube gap and breaker, the LLM budgets) and the schedules | `docker compose up db`, `catcher db upgrade`, then look at the tables. Migrations upgrade from empty and downgrade |
| B2 (built 2026-10-02) | **Our own queue module** on Postgres: `enqueue()`, `claim()` (`FOR UPDATE SKIP LOCKED`, `run_after`), `complete()`, a lease and heartbeat so a crashed worker's job comes back, `LISTEN/NOTIFY`. Azure-style semantics: `attempts`, `max_attempts` 5, a `dead` status, an invisible delay before a retry. **Priorities:** a new clip and a requeue you asked for by hand go first, a backfill goes last | Tests on **real Postgres with a fake clock**: two workers never claim the same job; a crashed worker's job comes back; backoff and `dead`; the priority order |
| B3 (built 2026-10-03) | Job handlers that call the Stage A functions: `pipeline.run`, `llm.reason`, `pipeline.publish`, and a separate **`youtube.fetch`** (the rate-limited resource, idempotent, saved facts) so that **an LLM failure never causes a YouTube call**. Every handler is idempotent (overwrite by id already helps) | `catcher jobs add pipeline.run` + `catcher worker` on a copy of the repos gives the same pages as Stage A. A failing LLM makes no YouTube call |
| B4 (built 2026-10-04) | **The YouTube gate moves into Postgres** (`resources`). The claim reserves the slot in the same transaction, so two workers can never break the gap. A 429 sets `blocked_until` and pushes `run_after` past it **without counting an attempt** | Fake clock: two workers cannot break the gap; a block stops every fetch; a working fetch closes the breaker |
| B4b (built 2026-10-04) | **One gate and one run lock, only in Postgres.** `run pipeline` and `youtube facts` go through the Postgres gate, and `run pipeline` takes the worker's lock. The file gate, `pipeline.lock`, `--import-file` and `CATCHER_STATE_DIR` are deleted | With the database stopped `run pipeline` and `youtube facts` exit 2 and change nothing; with a worker running `run pipeline` exits 2 |
| B5 (built 2026-10-06) | **State, retries and metrics.** `job_items.status` (waiting, deferred, stuck, failed...), `stuck` after 3 days, per-backend blocking on a quota error, and how the in-call LLM retries (5 calls) and the job-level `retry_delay` add up. The **frontmatter mirror** (`stage`, `stage_reason`, `stage_since`) is written on status changes only. A `catcher reconcile` rebuilds the database from the folders. The metrics queries | Set `run_after` and the clock in tests. Stop FreeLLMApi and check that jobs defer. Delete the database and reconcile. Query the metrics with SQL |
| B5b (built 2026-10-06) | **`run pipeline` over the worker.** The command queues `pipeline.run`, runs the worker in its own process until nothing is due, then queues and runs `pipeline.publish`, and prints the report from the database. `--dry-run` is a read-only preview. The Stage A loop is deleted | A parity table of the 72 behaviours of the old loop; every old test runs on the worker path; a live run on a throwaway Postgres and copies of the test repos |
| B6 (built 2026-10-07) | **The scheduler loop in the worker** with three cron variables: `SCHEDULE_IDEAS_PULL` (often), `SCHEDULE_PIPELINE_RUN` and `SCHEDULE_PUBLISH` (a few times a day), plus a manual `catcher publish`. A missed slot runs once | Fake clock: each schedule fires on time; a missed slot runs once; the pull runs more often than the publish |
| B7 (built 2026-10-07) | The worker in Compose next to `db`: the `repos` volume, the image (Python 3.12, `uv`, Git, `yt-dlp` + Deno) | `docker compose up`, then watch scheduled runs happen (`scripts/compose-smoke` does it on throwaway copies) |
| B8   | **Backfill import.** `catcher youtube import` finds the YouTube links in the docs, skips the video ids that already have a page, and adds the rest as **low-priority** jobs. A paced channel listing. An LLM budget or a daily `--limit` | Fixtures only in the tests. A hand test on a small channel |

**Built so far (2026-10-02): B0, B1 and B2.** What exists, and what changed from the plan:

- **B0, the steppable run.** `outcome.py` (the error ladder as `classify()` and `Outcome`), `steps.py` (`order_notes`, `split_duplicates`), `load_staged_note`, and `get_facts`, `ask_llm` and `build_page` in `process.py`; the run applies each `Outcome` in one place (`apply_outcome`, `RunState`, `log_outcome` in `run.py`; since B5b in `outcome.py`, with `run.py` deleted). Stage A behaviour is unchanged.
- **B1, the database.** `core/db.py`, `DATABASE_URL`, models and Alembic migrations `0001`, `0002` and `0003` in `migrations/`, `catcher db upgrade` and `catcher db downgrade`. Tables: `jobs`, `job_items`, `job_events`, `resources`, `schedules`. **`compose.yaml`** (added 2026-10-04) has only the `db` service for now (`docker compose up -d db`, a named volume for the data, a health check); it is documented in [How to Run Stage B](../idea-catcher-how-to-run-stage-b/#database). B7 adds the worker to it.
- **B2, the queue.** `modules/queue/queue.py`: `enqueue()` (with a `dedupe_key`), `claim()` (`FOR UPDATE SKIP LOCKED`, `run_after`, priority), `heartbeat()`, `complete()`, `fail()`, `defer()` and `reap()`. Following the decisions above, it has **no `dead` status** (a job that is out of attempts is `failed`), **no job-level backoff** and **no `LISTEN/NOTIFY`** (the worker will poll).
- **Migration `0003`, schema hardening.** CHECKs `ck_jobs_counters_non_negative` (`attempts`, `claim_seq` >= 0), `ck_jobs_running_has_lease` (a `running` job has a `lease_until`) and `ck_job_items_origin` (`inbox` or `backfill`); indexes on `job_events.job_id`, `job_events.item_id` and `job_items.root_job_id`; and the partial index `ix_jobs_running_lease` (on `lease_until` where `status = 'running'`) for the reaper. `catcher db downgrade` now needs a REVISION and asks before it goes to `base`.
- **Changed from the plan: the fencing token is `(locked_by, claim_seq)`, not `(locked_by, attempts)`.** `defer()` gives its attempt back, so after a defer the same worker's next claim had the same `(locked_by, attempts)` and a stale copy of the first claim could complete the second one. Migration `0002` adds `jobs.claim_seq` (increased by every claim); `0001` stays as reviewed. **`attempts` stays the crash counter**: a claim adds one, `defer()` gives it back, `reap()` does not change it and fails the job when `attempts >= max_attempts` (default 3).
- **Changed from the plan: the fenced update refuses a modified job and does not autoflush.** `heartbeat`, `complete`, `fail` and `defer` raise `ValueError` for a `Job` with unsaved changes, and the UPDATE runs without autoflush. Before this, a flush could write a stale caller's pending changes without the fence, and a stale `complete()` returned True. Other dirty objects in the caller's own session are still flushed when that session commits: pass the `Job` that `claim()` returned, in a session of its own.
- **Test isolation rule: the tables are truncated before every database test** (an autouse fixture in `tests/integration/db/conftest.py`; `alembic_version` is left alone). Tests that commit rows of their own used to leak running jobs into later tests that share a frozen clock. The tests run on a real Postgres 17 in a throwaway container.

**Built (2026-10-03): B3, the worker and its handlers.** What exists:

- **The worker** (`modules/worker/`): `catcher worker [--once] [--ideas] [--docs] [--lease-s] [--poll-s]` claims one job at a time and runs its handler; a heartbeat thread renews the lease while the handler runs; the reaper runs at the start and every 60 seconds between jobs; Ctrl-C or SIGTERM ends it after the current job. A Postgres advisory lock allows **one worker per database** (a second exits with code 2); the worker checks before every claim and reap that its lock connection still holds the lock and stops with exit code 1 when it does not (a Postgres restart, a dropped connection). Worker sessions have a `lock_timeout`.
- **The handlers** (`handlers_pipeline.py`): `pipeline.run` (stage the inbox, then queue the next job per document), `youtube.fetch` (saved facts, else one paced fetch through the Stage A gate; a closed gate defers the job to the gate's time without counting an attempt), `llm.reason` (saved facts only, the saved reply before the model, the page), `pipeline.publish` (the only handler that runs git). A handler returns `Done`, `Defer` or `Fail`.
- **The item API** (`modules/queue/items.py`): one `job_items` row per calculated name, with the status of the document (`staging`, `waiting_youtube`, `waiting_llm`, `published`, `deferred`, `failed`, `duplicate`...).
- **`catcher jobs add TYPE [--param KEY=VALUE] [--priority N]`** and **`catcher jobs list [--status] [--limit]`**. `jobs add` refuses an unknown type and params the handler's own parser refuses (exit 2); `only` and `requeue` are lists (repeat `--param` for more). How to use them: [How to Run](../idea-catcher-how-to-run/#worker-stage-b).
- **Tests:** a database harness with a frozen clock and fake services (`tests/support/`), the worker publishing the frozen real run of the test data with the same pages as Stage A, and crash injection at each step of each handler.

**The transaction rules.**

- **The claim commits before the handler runs.** A crash after that leaves a `running` job that the reaper recovers after its lease.
- **No database transaction is open while a handler calls a model, YouTube or git.** Reads and writes are short sessions before and after the call.
- **An item's new status and its next job are committed together** (one commit), so a document is never in a state with no job to move it on, and a crash between the two cannot queue a job twice.

**Changed from the plan** (rulings made while building; where a step or plan text disagrees, this list wins):

- **Adoption after a crash checks `inbox/` before `output/`.** A `staging` row whose capture is still in `inbox/` is rebuilt under the same calculated name, but only when the inbox file is the same document as `archive/<name>` (a new capture at the same path is staged on its own). Otherwise a copy in `output/` continues at the queue step.
- **Publish commits first, then `pull --rebase`, then push** (the plan said pull first). The pull's autostash then never holds the worker's own changes; a failed rebase is aborted and fails the job, the local commit stays and the next publish pushes it. Stage A's pull now does the same: it aborts a failed rebase, pulls with `merge.directoryRenames=false` (a capture added to a folder the worker emptied stays where it was added), and stops when the user's autostash cannot be put back cleanly.
- **Unattended git only for the worker.** `GIT_TERMINAL_PROMPT=0`, ssh `BatchMode` (only when no `GIT_SSH_COMMAND`, `GIT_SSH` or `core.sshCommand` is set), `http.lowSpeed*` and timeouts (about 30 s local, 600 s network; a timeout stops the whole process group) apply to the worker's git calls only. The Stage A CLI keeps its interactive git.
- **`youtube.fetch` jobs carry `profile` and `refresh_llm`** and pass them on to the `llm.reason` job they queue.
- **The reaper fails the items of a job it fails** and moves their working copy to `failed/` (after its commit, best effort), except items that were handed off to another queued or running job.
- **The `Done` counts of `pipeline.run` have a sixth key, `errors`** (`staged`, `adopted`, `duplicates`, `artifacts`, `unreadable`, `errors`).
- **Job and item outcomes are separate.** A deferred or failed document ends its job as `succeeded`; an unexpected exception in a handler fails the item (moved to `failed/`) **and** the job (no retry).
- **Any failed job fails its item** (final fix wave). After a job ends `failed` for any reason (a handler's `Fail`, a parameter error, an exception `run_job` caught), the worker runs the reaper's `fail_items_of` on it and moves the working copy to `failed/`, unless another queued or running job carries the item (the same hand-off guard). So a document is never left active with no job.
- **A requeue judges "in use" by the jobs, not the status.** `requeue` leaves an item alone while a queued or running job carries its calculated name; an active item that no job carries is a stuck leftover: it is marked `stuck` and requeued (a `staging` row is left to the adoption of the next `pipeline.run`).
- **Calculated names may have 1 or more folder parts.** A capture directly in `inbox/` is `<name>.md`, a nested one `clippings/2026/<name>.md`; `llm.reason` and `youtube.fetch` accept every name `assign_name` makes and refuse only what could leave the ideas folder (absolute, empty, `.`/`..` or dot parts, backslashes, NUL).
- **The publish commit set is all of `inbox/`** (`git add -A`, so new captures that arrived after the last `pipeline.run` are committed too: intended, the capture is then safe in the repo) **plus `llm/`** (the saved replies), on top of decision 6's `archive/ output/ failed/ duplicates/ facts/`.
- **The epiaku-docs commit set is exactly four folders** (2026-10-03): `hugo/content/en/docs/idea-bucket/notes/`, `.../youtube/`, `.../web-clips/` and `idea-bucket/artifacts/` (`DOCS_MANAGED`, built from `DESTINATIONS`, no longer from the doc types). An old `clippings/` folder is not in it, so a page left there is never committed.

**Decision 2026-10-03: the epiaku-docs layout has `notes/`, `youtube/` and `web-clips/` only.** The idea-bucket section of epiaku-docs loses its `clippings/` folder. Notes go to `notes/`; direct YouTube pages and Gemini chats about a video to `youtube/`; every other page (web clips **and** AI chats that are not about a video) to `web-clips/`. The working copy gets a frontmatter field **`destination`** (`notes`, `youtube` or `web-clips`) when it is analysed, set from the class; a valid `destination` already in the capture wins (written by hand; `archive/` keeps the capture as it was, so a requeue gets it again), an invalid one is ignored with a warning. `write_page` takes the destination and checks it again, so a value from a file never becomes a path unchecked. The finished page carries `destination`, `stage: published` and `created_by: idea catcher` too, so `output/` and the docs page show the folder and that the catcher made the page (changed 2026-10-03, later the same day; the other mirror fields `stage_reason` and `stage_since`, and the other states, come with B5). Republishing replaces the page with the same `id` only in the target folder: pages that sit in an old `clippings/` folder are the user's to move by hand once. Nothing changes in idea-bucket: `inbox/clippings/`, the `clippings` LLM profile, the classes, prompts and templates keep their names; an AI chat only changes folder. Artifacts stay in `idea-bucket/artifacts/`. The saved LLM replies still match: `content_key` hashes the task, the body and the transcript, not the frontmatter.

**Size check (decision 11).** `wc -l`: `queue.py` is **222 lines**; the whole `queue` package is **377 lines** (`queue.py` 222, `models.py` 155, an empty `__init__.py`). The rule "queue core about 300 lines" is about the queue logic, which is `queue.py`; `models.py` is the schema. **Verdict: under the threshold, so Procrastinate stays the fallback and we continue with our own queue.** No flaky concurrency test was reported in review, and the fencing holes found there were fixed (see above); the concurrency test does not yet force the blocking path or the rollback-by-winner path deterministically. B3 to B5 will add code to the queue, so look at the count again at B5.

**Minors carried into B3** (found in review, not fixed in B2; this is the full list, the review ledger is not kept; three are settled in B3, marked below):

*Schema and migrations*

- *Settled in B3:* `job_items.doc_id` and `doc_class` are NOT NULL: both are set by `_analyse` before staging, so every staged row (also `waiting_youtube`) has them.
- The drift test reads `information_schema.data_type`, which hides USER-DEFINED/ARRAY detail (irrelevant for the current text/json columns).
- The architecture's database section also lists `progress_done/total/message`, `trigger`, `queue` and `failed_days`, which the B1 tables leave out.
- Stage C's image must copy `alembic.ini` and `migrations/` (`alembic_config` uses `PROJECT_ROOT`).
- `migrations/` and `tests/` are not type-checked (pyright `include` is `src` only).
- `Settings.database_url` has a development password as its default: a deployed Stage C must fail loudly when `DATABASE_URL` is not set.
- The dedupe predicate text is defined twice (`_ACTIVE_DEDUPE` in `queue.py` and in `models.py`): make it one shared constant.

*Queue behaviour*

- *Settled in B3:* the reaper runs on a timer in the worker (at the start and every 60 seconds, between jobs) and logs an error instead of stopping the worker.
- `defer()` silently accepts a `run_after` in the past (document "due at once").
- `claim()` and `heartbeat()` check `lease_s` (finite, > 0); a `types=[]` matches nothing (undocumented).
- `with_for_update` needs `of=Job` if a join is ever added to `claim()`.
- `heartbeat()` after the lease expired but before a reap revives the lease (acceptable; documented in `reap()`).
- `_finish` is named misleadingly for heartbeat (the helper is `_fenced_update`; check no stale name is left).
- Other dirty instances in the caller's session (for example a merged copy) are still flushed unfenced when that session commits: add one docstring sentence to heartbeat, complete, fail and defer. A same-identity detached/dirty mismatch edge is unhandled.
- `reap()` overwrites unflushed changes on a dirty `Job` in the caller's session (use its own session). `max_attempts=0` runs once and then fails (acceptable).
- `enqueue()`: the returned `Job` differs between the key and no-key paths, `populate_existing` on the INSERT does nothing, and the retry-exhaustion `RuntimeError` is untested; there is no test for a naive `run_after`.

*Tests*

- *Settled in B3:* worker sessions have a `lock_timeout` (10 s). Still open: the thread `join(30)` in the queue tests is non-daemon and can stall pytest exit on failure.
- The concurrency test does not force the blocking path nor the rollback-by-winner path (add a deterministic holder-session test); its lock-wait poll is not scoped to the reaper (add `datname` / `pg_blocking_pids`).
- Check rejection tests insert a partial column set, so a NOT NULL violation would also raise `IntegrityError`; the `-x url=` path of `env.py` is untested.
- The db conftest: a broad `except Exception` turns an ImportError into a Docker skip (import outside the try, catch only the container start); the container is not stopped if `start()` fails partway or `make_engine` raises; the admin engine in `fresh_database_url` is created before the try; the session fixture lacks a return annotation; two `pg_engine` tests request `session` only for its teardown (comment it).
- Stale comment "reap comes in a later step" in `test_queue_finish.py` (a hand-rolled UPDATE could call `reap`); the poison-pair probe in `test_queue_reap.py` relies on file order; the test name "nothing is written" depends on the explicit `own.rollback()`.
- Runtime floors were tightened to the resolved versions (`sqlalchemy>=2.1.2`, `alembic>=1.20.0`).

*From B0 (steppable run)*

- `outcome.py`: the subclass-order test is redundant with the parametrized `BudgetExhausted` row. `steps.py`: the "does not touch files" test cannot fail without I/O, and no test asserts `winner.rel`.
- `inbox.py` (`load_staged_note`): `or rel == Path(".")` is redundant; no tests for a symlink out of the repo or a file directly in a folder. (The repo-root `IndexError` is fixed: it raises `ValueError`.)
- `process.py`: `str(vid)` and `str(gemini_video_id())` would embed "None" if `build_page` is called without `get_facts`; no test that `get_facts` raises with no transcript, or that `ask_llm` is not reached; a `FactsUnavailable` import sits inside a test function (`test_process_steps.py`).
- The Gate test "no state file written" checks a `tmp_path` that nothing points at (near-vacuous).
- `run.py`: no test pins the `log_outcome` levels and wording per kind (add a parametrized caplog test); `log_outcome` is called in two places with different message arguments (compute the message once); the dry-run test runs `start_work` first (cosmetic).

**Open items after B3** (deferred minors from the B3 reviews that matter later):

*For more than one worker (not done in B4: B4 made the gate safe for more than one worker, one worker is still the rule)*

- `fail_items_of` in the reaper checks the item status before it locks the row.
- The reaper's file move to `failed/` after its commit is not fenced: re-check `status == "failed"` just before the move.
- `GIT_LOCK` is per process, and only `pipeline.publish` takes it: use the `git` resource.
- The status read and write in `youtube.fetch` and a few other handler paths are not one transaction.

*For B5 (reconcile and item states)*: settled in B5 (a reset clears `stage_reason` and sets `stage_since`; only `deferred` items become `stuck`, never a `waiting_youtube` one). What is left is in "Open items after B5".

*Robustness*

- ~~**A down LLM backend is called once per queued `llm.reason` job**~~ (final review F4). Done 2026-10-04, and since B5 the blocks are rows in `resources` (see B5 below).
- `os.killpg` is POSIX only: fall back to `terminate()`/`kill()` on Windows.
- The ssh-config probe uses a plain `subprocess.run`, so a timeout or `OSError` there is not a `GitError`.
- `worker --once` spins on a handler that defers to a time already due (only with a gate time in the past). *Mostly settled in B4:* a fetch job that carries the `youtube` resource is no longer claimed while the gate is closed; a `youtube.fetch` added by hand with `jobs add` has no resource and is still claimed and deferred.
- **Decision 6's "once a worker exists, the CLI refuses to run against the live remote" is not built.** *Settled for one database in B4b:* `run pipeline` takes the worker's lock, so the two cannot run at the same time. A run and a worker on two different databases could still share one checkout.
- `refresh_llm=true` skips the saved reply by design, so a rerun after a crash pays the model again ("the model is paid once" holds without `refresh_llm`).
- Git's stderr goes into `job.error` and the logs (up to 500 characters): with a token in the remote URL (Stage C) use a credential helper, or redact `//user:secret@`.

*Tests*

- The heartbeat and lease tests with a 0.6 s lease can be flaky on a slow machine.
- The byte compare of `llm/` and `facts/` in the end-to-end test does not assert that the folders are not empty.

**Built (2026-10-04): B4, the YouTube gate in Postgres.** What exists:

- **One set of rules, two gates.** `modules/youtube/gate_rules.py` holds the rules as pure functions (gap plus jitter, the breaker 6/12/24 hours, "a newer block wins", a time more than 24 hours ahead is damage). The file gate (`gate.py`, the Stage A CLI, `CATCHER_STATE_DIR`) and the new `PostgresGate` (`pg_gate.py`, the worker) both used them, and one contract test suite ran against both (B4b deleted the file gate; the suite now runs against `PostgresGate` and an in-memory test double).
- **The row `youtube` in `resources`.** Migration `0004` inserts it open. `PostgresGate` locks it (`SELECT ... FOR UPDATE`, in a short transaction) and reads its clock after the lock. A missing row is inserted closed, a damaged one rewritten closed, with an ERROR in the log.
- **The worker uses it** (`build_context`, with the context clock for the gate and for the YouTube access); the Stage A commands kept the file gate until B4b.
- **The claim skips jobs whose resource is closed**, in the same statement (`FOR UPDATE SKIP LOCKED` stays): a waiting `youtube.fetch` job stays `queued`, counts no attempt and writes no event, the worker idles, and `catcher jobs list` shows `waiting for youtube until <time>`. Both places that queue a fetch job set `resource="youtube"`, and so does `catcher jobs add youtube.fetch` (the type-to-resource map `JOB_RESOURCES` in `worker/app.py`).
- **`catcher youtube gate`** shows the row; **`--import-file`** merged the old `youtube-gate.json` into it once (deleted in B4b). How to use them: [The YouTube gate](../idea-catcher-how-to-run-stage-b/#youtube-gate).

**Changed from the plan** (B4):

- **The handler reserves, the claim only skips.** The B4 row said the claim reserves the slot in its transaction. Instead the `youtube.fetch` handler reserves right before the fetch, in its own short transaction, and the row lock guarantees the gap; the claim only skips fetch jobs while the gate's time is in the future. Same safety, a simpler claim. A reserve that still gets a wait (a race, the jitter) defers the job to the slot, as in B3.
- **The claim's 24-hour horizon.** A resource time more than 24 hours ahead counts as open in the claim, so a damaged value cannot hold every fetch job back forever: the job is claimed, and the gate repairs the row (an end time cut to 24 hours, a `blocked_at` cut to now). `YOUTUBE_BLOCK_HOURS` is limited to more than 0 and at most 24, so a real block is never longer than the horizon.
- **A database error never opens the gate.** When the gate cannot read or write its row, no fetch is made and the job waits 60 seconds. A 429 whose block cannot be recorded makes the worker hold back for the first breaker step (`YOUTUBE_BLOCK_HOURS`) in memory, and the block is written into the row as soon as the gate answers again (before any other gate call), so `catcher youtube gate` and a restarted worker see it. Only a restart before the database answers again forgets it.
- **The import was a merge** (deleted in B4b). `--import-file` kept the larger of each value (the gap, the end of the block, `blocked_at`, the streak), so it never shortened a block or reset the streak.
- **A rebuilt database starts with the gate open.** Migration `0004` seeds the row open (a fresh install must not wait 6 hours), and its downgrade deletes the row. A database that is deleted and rebuilt, or downgraded and upgraded, during a block therefore loses that block; B5's reconcile closes the gate (decision 10).

**Open items after B4** (deferred minors from the B4 reviews that matter later):

*Robustness*

- The queue imports the 24-hour constant from the YouTube module: move it to a neutral constant in the queue when B5 adds a second resource.
- Fetch jobs queued before B4 carry no resource, so they are claimed and deferred instead of waiting (harmless: the handler's reserve still guards, and they resolve themselves).
- Edge cases of the Postgres gate, all failing closed: the repair's ERROR log comes before the commit; an int32-max streak in the row overflows on the next block. (The file-gate cases went with the file gate in B4b.)

*Tests*

- The two "gate unavailable" tests do not assert why (lock timeout or connection error), nor the ERROR line.
- `test_two_engines_reserving_together_get_one_go_ahead` is evidence of the row lock, not proof (it also passes when the threads happen to run one after the other); the lock is also pinned by `test_the_clock_is_read_under_the_row_lock`.

*For B5*: both settled in B5 (the LLM blocks are `resources` rows; `catcher reconcile` closes the gate).

**Decision 2026-10-04: Postgres is the only truth, with no file mode.** The user's direction: we do not build a system that works with or without Postgres; when the database is not running, the system does not run. The exceptions are `scan` and `reason` (not for YouTube clips), which read one document and write nothing; `run pipeline`, `render`, `youtube facts`, `youtube gate`, the worker and the `jobs` commands need it. This settles the "two gates on one machine" item of B4 (no best-effort guard: one gate) and the open import edge cases (the import is gone).

**Built (2026-10-04): B4b, one gate and one run lock, only in Postgres.** What exists:

- **One YouTube gate.** `build_access` always builds a `PostgresGate` on `DATABASE_URL`; `run pipeline`, `youtube facts`, `render` and the worker all go through it. The facts are reached only through the access (the default `Services.facts` raises). When the database cannot be reached no call is made: a clip waits with `YouTube gate unavailable`, and `youtube facts` exits 2.
- **One run lock.** `catcher run pipeline` (also `--dry-run`) takes the worker's advisory lock (`WorkerLock`) before it touches a file: exit 2 when the database cannot be reached or a worker or another run holds it. A run that loses the lock halfway stops before the next document, commits nothing, prints how many documents (and artifacts) it finished and exits 1; those stay uncommitted until the next `pipeline.publish` job. `render` takes the same lock (fix wave), so a worker's publish never commits a preview page. (Since B5b the command holds the lock for the whole run and checks it before each job; its own `pipeline.publish` commits everything under the managed folders, so the next `run pipeline` commits what a lost run left behind.)
- **Deleted:** the file gate (`YoutubeGate`, `youtube-gate.json`, the `.corrupt` handling), `catcher youtube gate --import-file`, the `pipeline.lock` file and the setting `CATCHER_STATE_DIR` (an old line in `.env` is ignored). `catcher youtube gate` still shows the row.
- **Tests:** the unit and component tests use an in-memory gate (`tests/support/memory_gate.py`) on the same rules, so they need no Docker; the network guard is on in every pytest run (off only for `CATCHER_ALLOW_NETWORK=1 uv run pytest -m live`, and then it says `BLOCKNET: guard OFF`).

**Open items after B4b** (from the final review; the fix wave settled the rest):

*For B5 (file-held truth that is left)*: the worker's half is settled in B5 (the database is the truth, the blocks are rows); the Stage A `run pipeline` kept its states in the folders and its blocks per run until B5b, which made it the worker path (see "Built: B5b").

- The in-memory hold of a 429 that the gate could not record (`YoutubeAccess.unrecorded_until`) is per process by design (it exists because the database was down): a `run pipeline` that ends forgets it.

*Its own task*

- **`run pipeline` does not commit what a lost run left behind**: it commits only the files it changed itself. Settled in B5b: the command's `pipeline.publish` commits like the worker (`commit_managed` over the managed folders), so it does, and it also commits hand edits under those folders.

*Deferred*

- No `connect_timeout` in `make_worker_engine`: a black-holed host (not a refused port) makes `run pipeline`, `render` and the gate wait for the OS TCP timeout (the worker too).
- `YoutubeAccess.fetch` is a public attribute holding the raw fetcher, and the AST tripwire (`test_no_fetch_without_the_gate.py`) checks names only; the raising `Services.facts` default is the real guard. Cheap hardening: rename it `_fetch` and pin `YoutubeAccess(` constructions.
- `testdata reset` takes no lock (it rebuilds `tmp/ic`; only matters when a worker runs on the test repos).
- No test for a worker started during a run (the same key covers it) or for the lock being released after Ctrl-C (the `ExitStack` and the connection's end cover it).
- `InMemoryGate` (test only): a shared `make()` changes the clock, rng and hours of the one gate; no clamp of a test-set state more than 24 hours ahead.

**Built (2026-10-06): B5, item states, metrics and LLM blocks in Postgres.** On the worker path the database is the truth. What exists (how to use it: [Item states, stuck and reconcile](../idea-catcher-how-to-run-stage-b/#item-states)):

- **One writer of item state.** `ItemStates.transition` (`modules/queue/states.py`) is the only code that changes `job_items.status` on the worker path: one commit writes the status, `stage_reason`, `stage_since` (only when the status really changes), `updated_at` and one `job_events` row; a repeated transition writes nothing. Migration `0005` adds `job_items.stage_since` and `resources.reason`.
- **The frontmatter mirror** (`pipeline/mirror.py`), after the commit, best effort: `stage`, `stage_reason`, `stage_since` on the working copy in `output/` or the file in `failed/`. The finished page keeps only `stage: published` and `created_by`.
- **Metrics on the item:** `llm_profile`, `llm_backend`, `llm_model`, `prompt_version`, `tokens_in`, `tokens_out`, `llm_duration_ms`, `warnings`, `docs_page` and `llm_result = {"attempts", "saved"}`; the state changes and warnings are events. The queries are in [Database & Metrics](#mvp-database).
- **`stuck`:** at every reap an item `deferred` for `STUCK_AFTER_DAYS` (3) becomes `stuck`; `retry_deferred` retries `deferred` and `stuck` items and the active items no job carries. **`catcher items list [--status S] [--limit N]`** shows them.
- **LLM blocks are `resources` rows**, shared by every worker and kept over a restart: a usage limit or a down backend `LLM_BLOCK_S` (600 s), a used-up budget `LLM_BUDGET_BLOCK_S` (6 h), a wrong model only its profile (`<backend>:<profile>`).
- **`catcher reconcile [--ideas PATH] [--dry-run] [--keep-gate]`** rebuilds the rows from the folders, never deletes a file or a row, and closes the YouTube gate.

**Changed from the plan** (B5):

- **The mirror names sit next to the Stage A names.** The worker writes `stage`, `stage_reason`, `stage_since` on top of what the shared Stage A helpers write (`analyzed_at`, `deferred_at`, `deferred_reason` stay); the readers and reconcile understand both. The Stage A loop is unchanged.
- **`stage_since` is the time of the last real status change.** A (re)staged item gets a fresh `stage_since` and an empty `stage_reason`; a `stuck` item keeps the time it became stuck, also when a retry defers it again.
- **Reconcile closes the gate without growing the breaker** (streak at least 1, never a shorter block), and it closes it before it writes the rows. Each run without `--keep-gate` closes it again from now.
- **The wrong-model rule is narrower than the plan** (ruling on the Task 5 review). The plan said any 4xx that is not auth or quota blocks only the profile. The code does that only for an HTTP 400 or 404 whose error code or text says the model is unknown; any other refused request blocks the whole backend for `LLM_BLOCK_S`, as before. Widen it only if we see it happen.
- **A missing LLM row means open**, unlike `youtube` (missing means closed): an LLM block is only ever made by a failure. A failed read of the rows blocks every known backend for 30 s (fail closed); a failed write is logged, never raised. The `youtube` name cannot be blocked or unblocked through the LLM blocks.
- **The `stuck` rule:** only `deferred` items become `stuck` (never `waiting_youtube` or `waiting_llm`); the requeue's own "active item with no job" leftover uses the same `stuck` status. Rows rebuilt by reconcile in an active status get no job: a `pipeline.run` with `retry_deferred` picks them up (every `waiting_youtube`, `waiting_llm` or `ready` row no queued or running job carries, as well as `deferred` and `stuck`), from the archive copy or, without one, from the working copy in `output/`; a row with neither is left and named in a warning. `requeue=NAME` works from the archive copy only.
- **The stuck clock reads the event data.** Every transition event carries `{"from", "to"}` in its data (reconcile's created event too); `stuck_since` reads that, and the message only for events written before the B5 fix wave.

**Open items after B5** (from the B5 reviews; the final review adds its own):

*Robustness*

- A row holds only its last LLM run: a published item that is requeued and deferred mixes the new profile and backend with the old model and tokens, a requeue served from a saved reply drops the earlier live tokens, and the tokens of failed calls are only in the `llm/` trace. Cost queries read `published` rows only and undercount failed calls. Fix: put the tokens and the tried profile in the event data.
- If writing the metrics fails after the page is made, or the worker crashes between the page and its commit, the retry publishes with no metrics.
- Rows written before B5 may hold JSON `null` in `warnings` instead of SQL NULL: `UPDATE job_items SET warnings = NULL WHERE warnings = 'null'::jsonb`.
- The mirror: last writer wins (fine with one worker); on the file-error paths the item is `failed` while its file stays where it was with its old `stage`; a finished page that the reaper moves to `failed/` still says `published`.
- The blocks: `blocked_at` moves to now even when the longer, older block is kept (LLM rows and the gate); expired rows are never removed; there is no command to lift a 6 h budget block early or to show `resources.reason` (use SQL); the wrong-model regex stops at a dot; a missing API key blocks its backend for `LLM_BLOCK_S` too.
- Reconcile: a frontmatter `id` is stored as written (not through `safe_id`); after a fix the old path column is kept; rebuilt rows have no `docs_page`, no metrics and a `stage_since` of the reconcile time.
- A brief error reading the blocks from Postgres defers every document in that 30 s window (fail closed); they then need a `retry_deferred` (since B6 every scheduled `pipeline.run` carries it).
- Reconcile on a live database turns a `stuck` row back into `deferred`, with a fresh clock, when the `stuck` mirror was never written (the folder wins).
- A capture that cannot be analysed gets a doubled reason in its `failed/` file: `cannot read the capture: cannot analyse: ...`.
- A dropped-tags event is written again when only the other warnings of the item change; `stuck_since` reads the item's whole event history at every deferral (fine at today's sizes); the wrong-model block key uses the error's backend while the check uses the profile's.
- `retry_deferred` with a small `limit` moves the retried documents back to `inbox/` but stages only `limit` of them: the others wait there while their row keeps its old status, until the next `pipeline.run`.

*Tests*

- `ItemStates`: no test for the `level` check or two sessions at once; no test that a repeated transition does not rewrite the file.
- The blocks race test cannot fail (`block()` swallows errors): assert no ERROR log and swap the ends over several rounds. A stale comment in `test_handler_llm_reason.py` still talks about in-memory blocks.
- Reconcile: no test for a failing gate close, nor for the CLI's `--dry-run` leaving the rows alone. The worker with stand-in settings marks nothing stuck and logs nothing about it.

**Built (2026-10-06): B5b, `run pipeline` over the worker path.** There is one path now: only the worker handlers process documents. What exists (how to use it: [`run pipeline`](../idea-catcher-how-to-run/#run-pipeline-the-whole-flow)):

- **`catcher run pipeline` is a thin wrapper** (`modules/worker/runner.py`). It takes the worker's lock and holds it to the end, queues one `pipeline.run` with the options as params, runs the same `Worker` that `catcher worker --once` runs until no job is due (the run's own `youtube.fetch` and `llm.reason` jobs, and any queued document job of an earlier run), then queues and runs `pipeline.publish` with `pull = push` (without `--push` only local commits, no network). The report is built from the database (`report_for_job` in `pipeline/report.py`): the run's items plus the names the `pipeline.run` job returns in its result (duplicates, artifacts, unreadable files, unmatched names, requeue moves and skips, the documents `limit` left in `inbox/`, captures whose item is still active). After the run it prints one line per blocked LLM backend, read from `resources`.
- **`--wait-youtube`**: after the drain, while a `youtube.fetch` of this run becomes due within `YOUTUBE_WAIT_MAX_S`, it sleeps until then and drains again; a longer wait stays queued for a later worker.
- **`--dry-run` is a read-only preview** (`pipeline/preview.py`), not a job (decision 12): it reads the saved facts and replies, builds each page in memory, and writes no file, no row and no job; it never calls a model or YouTube and does not touch the gate (`would_call_llm`, `would_fetch`).
- **Ctrl-C or SIGTERM** puts the job the command was running back to `queued` (no attempt counted), leaves the rest queued, publishes nothing, prints `interrupted: N job(s) left queued, run catcher worker --once to finish` and exits 1.
- **An earlier run's `pipeline.run` or `pipeline.publish` still queued** makes the command refuse to start (exit 2, nothing done): it would run with its own params. Queued document jobs of an earlier run are fine.
- **The lock is checked per job** (`Worker.run_once`); a lost lock stops the run before the next job, publishes nothing and exits 1.
- **The old loop is gone:** `pipeline/run.py` (`run_pipeline`, `RunOptions`, `RunLockLost`, the `lock_check` option) is deleted. The helpers the handlers use moved: `apply_outcome`, `RunState` and `log_outcome` to `pipeline/outcome.py`, `copy_artifacts` to `pipeline/publish.py`, `NOT_STARTED` to `pipeline/report.py`; `core/git.commit_paths` (only the loop used it) is deleted. Every old test runs on the worker path through `tests/support/run_on_worker.py`.

**Dropped on purpose** (the parity table `docs/superpowers/plans/2026-10-06-idea-catcher-stage-b5b-parity-table.md`, and the user's decisions of 2026-10-06):

- **The commit messages lose their counts** (B3): `pipeline.publish` writes fixed messages (`idea-catcher: process the inbox (pipeline.publish)`, `idea-catcher: publish pages (pipeline.publish)`).
- **No pull before the run** (B5): the publish commits both repos first, then pulls with a rebase and pushes (only with `--push`); a failed pull fails the publish job and leaves the commit on the branch.
- **No per-note budget count** (B19): the first deferred note says `budget reached`, the next ones that the backend is blocked; one summary line per blocked backend replaces the `N note(s) waiting` ERROR line.
- **No progress lines** (B21): `(1/2) ... processing` and `processed 2/2` are gone; the worker logs its own lines (a `--dry-run` keeps `processed N/M`).
- **No comparison of the worker with the old loop** (B71): the golden-page comparison (B69) is the stronger check.
- **The lock is checked per job**, and the checkpoint "before the artifacts" is gone (B57).
- **Ctrl-C leaves the command's jobs queued and does not publish** (B56); the old loop committed what was done and exited 2.
- **A clip waiting for YouTube leaves `inbox/`**: it waits staged in `output/` with its fetch queued, and counts against `--limit` (B46).

**Open items after B5b** (minors from the B5b reviews):

- *The command:* a failed publish shows `committed={}`; `--wait-youtube` takes no value and also sleeps through a breaker block when it is short enough; Ctrl-C right after a successful publish gives `Aborted!` with no summary; a tiny window can print `interrupted: 0 job(s)`; a job error exits 1 with no printed reason; the SIGTERM handler is not restored afterwards; a stale `running` job (lease not expired) is called "queued" in the refusal; the run's `waiting` names, `left_queued` and blocked lines are not scoped to this run (with `--file`/`--requeue` they can list other runs' documents, possibly twice); `runner.py` says "the next publish pushes them", which is wrong for a repo without an upstream or after an aborted rebase. Seen once in the live check: after Ctrl-C, SQLAlchemy can print `Exception ignored in ... InstanceState._cleanup ... AssertionError` on stderr (the interrupt lands in its cleanup); the exit code and the output are right.
- *Publish:* when the idea-bucket push fails and then the docs pull fails, only the docs error is reported; a `pipeline.publish` added by hand shows `succeeded` after a failed push.
- *The preview:* its log line "moved to failed/" is wrong in a preview; `would_fetch` hides a closed gate or a ban; `would_call_llm` ignores budgets and blocked backends.
- *Code left from the loop:* `RunState`, `outcome_message` and the `dry_run` branch of `apply_outcome` only matter for one document at a time now (the handlers pass a fresh state); `YoutubeAccess.wait_needed` has no caller in `src` any more (only tests use it as a read-only probe). Both could be simplified.
- *The report* (`report_for_job`): it shows the CURRENT state, so an item a later run re-stages leaves this run's report, and past 500 adopted names items drop out (one `truncated` flag); adoptions that end `failed` or `error` get no report line; a retried `pipeline.run` keeps only the last attempt's names; artifact and unreadable messages can hold absolute paths (as Stage A's did); an already-active fetch job swallows a later `refresh_facts` silently; the not-found warning's wording changed from Stage A.
- *Final review:* the `RUN_LOST` text is wrong in a tiny window; the docs say "Ctrl-C: nothing is committed" but an interrupt during the publish leaves a publish that `catcher worker --once` then commits and pushes; `--push` now runs git without prompts, so a push that needs a passphrase ends "committed but not pushed"; `--dry-run --retry-deferred` reads the frontmatter while the real run reads the database; `ProcessOptions.wait_youtube` and YouTube's in-process wait have no caller in `src`; stale comments and dead interactive-git code in `core/git.py`; a second Ctrl-C while the git child is being stopped escapes `_stop_group`.
- *Blocks:* a `catcher blocks` command to list and lift LLM blocks (decision on B19/B20); until then use SQL on `resources`.
- *Tests:* the `SAME_ID` message in the report is untested; the B5b parity test of the report covers one inbox shape (no requeue, limit, waiting or `retry_deferred` recovery); the local commit is not asserted in the lock test at `test_run.py` ~927; style leftovers from the migration (`opts=dict(...)`, an unused `caplog`); the invalid-saved-reply path (B54) is untested; the CLI tests do not assert that no rows are written; the lock-lost exit-code unit tests are tautological and the `n=2` lock test lacks a `lock_lost` assert; a `pull=False` mutation survives; stale test names; a literal `interrupted` line in `run_on_worker.py`; a vacuous `in p.name[:0]` check in `test_run.py`; the database tests moved in B5b, so the pre-commit hook (unit and component only) does not run them.

**Built (2026-10-07): B6, the scheduler in the worker.** What exists (how to use it: [Scheduling](../idea-catcher-how-to-run-stage-b/#scheduling)):

- **`modules/scheduler/`**: `schedule.py` is pure (`ScheduleSpec`, `parse_schedules`, `load_timezone`, `due_slot`: the latest slot after `last_fired_at` and at or before now, slots computed in the schedule timezone with `croniter`); `loop.py` is the `Scheduler`: per schedule one transaction that locks its `schedules` row, queues the job with the dedupe key `schedule:<name>` and sets `last_fired_at`. `run_forever` ticks every `SCHEDULE_TICK_S` (30 s) and logs a failing tick once per failure streak.
- **`catcher worker`** (not `--once`) parses the schedules before it takes the lock (a bad cron or timezone: exit 2, the message names the variable), then runs the scheduler in a thread on the worker's engine, logs one line per schedule, and stops and joins the thread when the worker stops.
- **A new job type `ideas.pull`** (no params): `git pull --rebase --autostash` on `idea-bucket`; no remote is a no-op, a failed pull fails the job and leaves no rebase in progress, a rebase started by hand is never aborted.
- **`catcher schedules`** (read-only): name, cron, timezone, last fired (UTC), next due (schedule timezone; `due now`, `never`, `off`).
- **`catcher publish [--push]`**: the publish half of `run pipeline` (shared lock, refusal and drain code in `runner.py`); one `pipeline.publish`, never a `pipeline.run`; per-repo committed and pushed lines, a failed push reported per repo with exit 1.
- **Live check (2026-10-07)** on a throwaway Postgres and copies of the test repos with local bare remotes, all three schedules at `* * * * *`: the first fire came at the next minute, a pushed capture was pulled and staged, a stop of 18 minutes (and a laptop sleep of 16) gave exactly one job per schedule, SIGTERM stopped the worker in under a second (exit 0), and `catcher publish` refused while the worker ran.

**Decisions made in B6** (plan defaults, accepted by the user on 2026-10-07):

1. **Timezone default `Europe/Amsterdam`** (`SCHEDULE_TIMEZONE`, validated with `zoneinfo`; `tzdata` is a dependency so the image has the zones).
2. **A new schedule starts "now"**: with no row the scheduler stores `last_fired_at = now` and fires nothing; after downtime the row exists, so exactly one catch-up fires.
3. **Job types and params**: `SCHEDULE_IDEAS_PULL` queues `ideas.pull`; `SCHEDULE_PIPELINE_RUN` queues `pipeline.run` `{"retry_deferred": true}`; `SCHEDULE_PUBLISH` queues `pipeline.publish` `{"pull": true, "push": true}`.
4. **Dedupe**: key `schedule:<name>`; when the previous job is still queued or running no second job is queued, but `last_fired_at` still advances (the slot is consumed).
5. **Manual `catcher publish [--push]`** mirrors `run pipeline`: commit only unless `--push` (then pull and push).
6. **A bad cron string (five fields only) or timezone** stops `catcher worker` before it takes the lock, exit 2, naming the variable.
7. **Clock goes backwards** (`last_fired_at` in the future): nothing fires until now passes it; no error.
8. **Daylight saving time**: a slot in the spring-forward gap fires once at the shifted time (02:30 fires at 03:30); on the fall-back day a slot fires once, at its first occurrence, so a `*/30` schedule skips the repeated hour once a year (accepted).

**Open items after B6** (minors from the B6 reviews and the live check):

- *Scheduler:* the stop event is checked only between ticks, so a held `schedules` row lock can delay shutdown by up to the engine's 10 s lock timeout per schedule; in one tick each schedule takes its own `now`, so on a shared slot one schedule can fire a tick (30 s) later than another (seen live); an unreachable `except` in `run_forever`; a `time.sleep(0.1)` in a test.
- *Order inside a slot (live check):* a `pipeline.publish` that shares a slot with `pipeline.run` runs before that run's `llm.reason` jobs, so the pages go out with the next publish; a document that keeps deferring is re-staged by every scheduled run and makes a small `idea-bucket` commit at every publish.
- *Worker and CLI:* `catcher schedules` shows `02:30` as next due for a spring-gap slot that fires at 03:30; its `last fired` (UTC) and `next due` (local) columns do not say their timezone in the header; the start line says `next HH:MM` even when a missed slot fires at once; a lost worker lock is noticed only between jobs, so the scheduler may still queue meanwhile (harmless: dedupe and the row lock); `catcher schedules` on a database without tables gives a traceback (like `jobs list`).
- *Ideas.pull:* it commits the worker's managed folders locally first, so a pull every 30 minutes can make many small local commits in `idea-bucket` (all pushed by the next publish).
- *Tests:* a test uses `pytest.raises(Exception)` with a `noqa`; the lost-lock-before-publish message of `catcher publish` is untested.

**Built (2026-10-07): B7, the worker in Compose.** What exists (how to use it: [Run it in Docker](../idea-catcher-how-to-run-stage-b/#docker)):

- **Image** (`Dockerfile`): Python 3.12, `uv`, Git, `yt-dlp` and Deno; no Node, Claude or Hugo. It runs as the non-root user `catcher` (uid 1000). `.dockerignore` keeps `.env` out of the build.
- **`compose.yaml`**: `db`, a one-shot `migrate` (`catcher db upgrade`) and `worker` (`catcher worker`, `restart: unless-stopped`, the `repos` volume at `/data/repos`).
- **Decisions** (plan defaults, accepted by the user on 2026-10-07): https remotes with a fine-grained token through `GIT_ASKPASS` (`scripts/git-askpass.sh` answers only for GitHub over https; a remote with credentials in the URL is refused); the repos are cloned on first start by `scripts/docker-entrypoint.sh` (a folder with no `.git` is never touched); `migrate` uses `entrypoint: []`; `DB_PORT` and `DB_PASSWORD` in `.env` (defaults 5432 and `catcher`); the healthcheck is `catcher health` (new: exit 0 when a worker holds the lock, 1 when none does, 2 when the database cannot be reached; it reads only its own database); `stop_grace_period: 120s`.
- **A real bug the smoke run found:** `migrate` inherited the image entrypoint, which clones both repos, so every `up` made two throwaway clones and a failed migrate whenever GitHub or the token failed. Fixed with `entrypoint: []` and a unit test.
- **`scripts/compose-smoke`** (with `compose.test.yaml`): a real run on throwaway copies (own project, image tag and port, bare repos as remotes, no `.env`, no LLM, YouTube or GitHub). Run 5 (2026-10-07) passed: migrate, healthy worker, both clones, all three schedules fired, a capture pulled and the publish pushed, no token leak, the one-worker refusal, a clean stop, and after 285 s down exactly one catch-up job per schedule. `--selftest` checks its assertions without Docker.

**Open items after B7** (minors from the B7 reviews and the smoke run):

- *Image:* the Deno zip is downloaded without a checksum check (Deno publishes a `.sha256sum`); the base image is not pinned by digest; `yt-dlp` is declared without the `default` extra and without `yt-dlp-ejs` (Deno is on the PATH): check on the first real YouTube fetch whether the EJS solver needs `--remote-components ejs:github` or the extra; no real YouTube fetch ran from the container.
- *Volumes:* a bind mount on `/data/repos` owned by another uid is not writable by the `catcher` user (a named volume works).
- *Stop and health:* a stop is proven only with an idle worker (not with a long job in the 120 s grace period); `catcher health` proves only that a worker holds the lock, not that the scheduler thread ticks, and the unhealthy case was not run live.
- *Clone:* a `.git` alone counts as a finished clone (delete the folder to clone again); an scp-style remote (`user@host:path`) is not refused (it carries no password); no ssh deploy key; no `deploy.sh` and no `api` service yet (a later stage); a Mac-local FreeLLMApi needs `host.docker.internal` as `FREELLMAPI_URL`.
- *Smoke builds:* they leave BuildKit cache behind (`docker builder prune` clears it).
- *Tests:* two tests in `tests/component/test_testdata.py` inherit `GIT_*` variables and fail under the hook's temporary `GIT_INDEX_FILE` when a commit names explicit paths; commit with a plain `git commit` after `git add`.

**Done when:** jobs added by hand or by the schedule process the inbox exactly like stage A, failures defer and recover, the YouTube gap holds with more than one worker, and the metrics tables answer the questions in [Database & Metrics](#mvp-database).

#### Stage C: the API, locally {#mvp-stage-c}

**Goal:** start runs and read the results over HTTP.

| Step | What we build | How we test it |
| ---- | ------------- | -------------- |
| C1   | FastAPI app with `/health` (including the worker's heartbeat), API keys + scopes from `.env` | Swagger UI at `http://localhost:8000/docs`. A request without a key must fail |
| C2   | `POST /api/v1/pipeline/runs` (with dedupe: no second run while one is queued or running) and `GET /api/v1/jobs/{id}` | Start a run with `curl`, poll the job until it's done |
| C3   | `GET /jobs` and `GET /items` (filters for class, status, `stuck`), the manual **publish** and **requeue** triggers, and the YouTube gate state | Compare the answers with direct SQL queries |
| C4   | The `api` container in Compose | `docker compose up`: the full stack (`db`, `api`, `worker`) on the Mac |

**Done when:** the whole MVP runs locally with `docker compose up`, and everything can be started and checked through the API.

#### Then: Proxmox {#mvp-stage-deploy}

1. Create the LXC (Docker, `nesting=1`, `keyctl=1`), write `deploy.sh`, and put `.env` on the LXC with the GitHub PAT and `OPENAI_API_KEY`. `.env` is never rsynced.
2. Deploy, and run with the schedule against the real repos. After each deploy, a smoke test: `/health`, the worker's heartbeat, the migrations at head, and a dry-run job.
3. Backups: a nightly `pg_dump` and a Proxmox `vzdump` of the LXC.
4. Run the [model test suite](../idea-catcher-pipeline/#test-suite) against the profiles and adjust them.

After that, the [future features](#future) are added one by one in the same way.

#### Order and risks for Stage B and C {#mvp-stage-order}

1. **B0 first.** Everything else needs the run to be callable one step at a time. It is the riskiest refactor, so it comes with the Stage A tests as the safety net.
2. **B2 and B4 together.** The queue and the YouTube gate share one transaction, so the hardest tests (two workers, a fake clock, a crash) are there.
3. **Watch the size of the queue core.** If our own queue grows past about 300 lines or shows concurrency bugs, switch to **Procrastinate** (`lock="youtube"` plus our limiter table) instead of maintaining it, as decided in [YouTube IP bans and the queue](../idea-catcher-youtube-bans-and-queue-options/).
4. **Settle the two retry layers in B5**, so a failing provider cannot make one document take many minutes before it defers.
5. **Stage C is small** once the queue exists. Most of its value is already in B3 and B5.
6. **Test with real Postgres in Docker**, never with a mock of the queue: the claim and lease logic is where the bugs would be.

---

## 🔵 Part 2: Future Features {#future}

Each feature below is a new module or an extension of the MVP. None of them changes the MVP's job model, queue or tables. Several need the [Dashboard metrics API](#f-metrics) and the [management API](#f-management), which are listed separately.

| #  | Feature                                  | Adds                                                                                  | Depends on    |
| -- | ---------------------------------------- | ------------------------------------------------------------------------------------- | ------------- |
| F1 | [React front end in Hugo](#f-frontend)   | Web components, `web-app` shortcode, `/svc/` proxy, Caddy, live progress              | F4, F5        |
| F2 | [YouTube summary endpoint](#f-youtube)   | `POST /youtube/summaries`, cache, verify checks, `youtube-summarizer` component       | F3 (Gemini, optional) |
| F3 | [More LLM backends](#f-llm)              | `anthropic-api`, `gemini-api`, a **budget guard** (soft limit, warning at 80%, notification), per-backend concurrency         | –             |
| F4 | [Dashboard metrics API](#f-metrics)      | `/metrics/*` aggregation endpoints, React dashboards                                  | –             |
| F5 | [Management API](#f-management)          | Replay, cancel, retry, profiles and schedules in the DB, API keys in the DB           | –             |
| F6 | [Scaling out](#f-scaling)                | Separate `llm-worker` container, parallel jobs                                        | –             |
| F7 | [Other additions](#f-other)              | Map-reduce for long inputs, Hugo build check, GitHub App, mini AI chat, web clips, notifications | –   |

### 🖥️ F1: React Front End in Hugo {#f-frontend}

**Decision: React apps as web components inside Docsy pages, with dashboards built in React.** The docs site is static (Hugo + Docsy, served from `10.10.60.7`). Hugo can't run React server-side, but a page can load and mount a React bundle in the browser.

- Each app is built with **Vite in library mode** and wrapped as a **custom element** (`@r2wc/react-to-web-component`) with **Shadow DOM**, so Docsy's Bootstrap CSS and the app's CSS don't clash.
- A Hugo shortcode `{{</* web-app name="idea-dashboard" */>}}` loads the script and renders `<idea-dashboard>`. A mini AI chat later is just another tag.
- The same bundle also mounts in a bare `index.html`, so every app also runs **full-screen**.
- **Loaded at runtime from the service**, so apps deploy independently of the docs site and the Hugo build needs no Node/React. The docs web server proxies `/svc/` to the LXC (`location /svc/ { proxy_pass http://<idea-catcher-lxc-ip>/; }`), so the browser sees one origin and **no CORS** is needed. A **Caddy** container on the LXC serves the bundles under `/apps/` and proxies `/api/`.

```html
<!-- hugo/layouts/shortcodes/web-app.html (sketch) -->
{{- $name := .Get "name" -}}
{{- $base := site.Params.appsBase | default "/svc/apps" -}}
<div class="web-app-host">
  <{{ $name }} api-base="{{ site.Params.apiBase | default "/svc/api/v1" }}"></{{ $name }}>
  <noscript>This tool needs JavaScript.</noscript>
</div>
<script type="module" src="{{ $base }}/{{ $name }}/{{ $name }}.js"
        onerror="this.previousElementSibling.innerHTML='<div class=&quot;alert alert-warning&quot;>The idea catcher service is offline.</div>'"></script>
```

**API keys from React.** The React apps call the API with `fetch()`, which sends `Authorization: Bearer <key>` like any other client. A component asks for the key once and keeps it in `localStorage` (shared by all apps on the docs origin). Use a `read` key on devices that only view dashboards. The only browser limit is `EventSource`, the built-in helper for live progress streams (Server-Sent Events), which can't send headers. So live progress uses **polling** `GET /jobs/{id}` (enough at first), or a `fetch()`-based stream reader such as `@microsoft/fetch-event-source` for a live log view.

**Front-end workspace** (in the service repo):

```text
frontend/                       (pnpm workspace, Vite, TypeScript)
├── packages/
│   ├── api-client/             ← generated from the OpenAPI schema (openapi-typescript / @hey-api/openapi-ts)
│   └── ui/                     ← shared components, key prompt, theme tokens (light/dark follows Docsy)
└── apps/
    ├── idea-dashboard/         ← custom dashboards: runs, docs per type, warnings, tokens, stuck notes, "Run now"
    ├── youtube-summarizer/     ← paste a URL, pick a profile → progress → markdown preview + copy
    └── ai-chat/                ← later: mini chat
```

Libraries: React, TanStack Query, Recharts (charts), a markdown renderer. **Prove the pattern with one small app first** (the YouTube summarizer), because Shadow DOM needs care with modals, fonts and dark mode.

### 🎬 F2: YouTube Summary Endpoint {#f-youtube}

The MVP already fetches YouTube facts for clips and runs the LLM step on them. F2 exposes this as an endpoint that takes a URL and returns markdown, so no Obsidian clip is needed:

```text
 URL ─▶ 1. PARSE      video ID (watch, youtu.be, shorts, embed URLs)
        2. CACHE?     same video_id + prompt_version done before → return it
        3. FACTS      yt-dlp (counts, description, chapters) + transcript  ← MVP code
        4. REASON     llm.reason on the youtube.md prompt, one call         ← MVP code
                      + Python checks: timestamps, tools in transcript,
                      metrics only from step 3 → warnings, not a crash
        6. RENDER     Hugo page markdown (same template as the pipeline)
        7. OPTIONAL   publish=true → hand it to pipeline.publish
```

- `POST /api/v1/youtube/summaries { url, publish?: false, focus?, llm? }` → `202 { job_id }`. The markdown comes from `GET /jobs/{id}` or `GET /youtube/summaries/{video_id}.md` (`text/markdown`).
- `?wait=90` holds the request up to 90 s and returns the markdown directly if it's ready. This works because every profile is an API that answers within seconds.
- A `youtube_cache` table (video ID, prompt version, facts, markdown).
- Videos **without captions**: the `gemini-api` backend (F3) with native video understanding.
- An iOS Shortcut on the share sheet can call the endpoint over the VPN.

### 🧠 F3: More LLM Backends {#f-llm}

| Backend         | Use                                                                   |
| --------------- | --------------------------------------------------------------------- |
| `anthropic-api` | Pay-per-token Claude, native structured output, exact token counts    |
| `gemini-api`    | Gemini Flash; native video understanding for videos without captions  |
| `openai-api`    | Other models                                                          |

Paid backends come with a **budget guard**: the metrics know each call's tokens and cost, so the service compares the month's spend with a configured budget per backend, logs and notifies at **80%**, and defers jobs (as `budget reached`) before the key's own cap is hit, a **cost price table** for `cost_usd` in the metrics, and **per-backend concurrency** slots. They are selected **explicitly** by a profile or a message. They are **never an automatic fallback** for another profile's job.

### 📊 F4: Dashboard Metrics API {#f-metrics}

Aggregation endpoints on the MVP tables, so the React dashboards stay simple:

| Endpoint                                           | Returns                                                                 |
| -------------------------------------------------- | ----------------------------------------------------------------------- |
| `GET /metrics/overview?from=&to=`                  | Last run, next run, queue depth, waiting / deferred / stuck counts      |
| `GET /metrics/timeseries?metric=&bucket=day`       | Runs per status, docs per class, warnings/errors, tokens per model      |
| `GET /metrics/durations`                           | p50/p95 per job type and per backend                                    |
| `GET /metrics/tags`                                | Tag suggestions to review                                               |

### 🛠️ F5: Management API {#f-management}

- `POST /items/{doc_id}/replay` (optionally with another profile), `POST /jobs/{id}/cancel`, `POST /jobs/{id}/retry`.
- **Profiles and schedules in the DB** (`llm_profiles`, `schedules` tables), editable through `/llm/profiles` and `/schedules`, with an `admin` scope.
- **API keys in the DB**, stored hashed, managed through `/keys`.
- A generic `POST /llm/reason` for other tools that want reasoning through the same profiles.

### 📈 F6: Scaling Out {#f-scaling}

- Split LLM jobs into their own **`llm-worker`** container (`catcher worker --queue llm`). Then the Git worker never holds LLM tokens, and the LLM worker never holds the GitHub token.
- **Parallel LLM jobs** with per-backend slots (for example `freellmapi` 2, `openai` 4).
- **Parent/child job trees** with a `waiting` status, so a run shows as "done" only when all its notes are published.

### ➕ F7: Other Additions {#f-other}

- **Map-reduce for long inputs:** split long transcripts or chats into chunks, summarize each, then combine. Needed as soon as free models handle long texts.
- **Hugo build check** before pushing (Hugo extended + PostCSS in the image).
- **GitHub App** (`epiaku-docs-bot`) instead of a fine-grained PAT.
- **Mini AI chat** module (streams directly, reuses the LLM backends) and its web component.
- **An agentic checker** for any class, that can use tools (for example open the source page or search) instead of a single call.
- **Notifications** for stuck notes (for example a push message), instead of only the log and metrics.
- **`catcher cleanup`** for old `facts/`, `llm/`, `archive/` and `output/` records: a dry run first, by age and status. It must warn that deleting saved facts causes a YouTube refetch on a requeue, and that deleting saved replies costs LLM calls.
- **Saved replies, deferred items:**
  - `TraceStore.find` scans all trace files on each call. Add an index, or look at the document's own path first, when there are thousands.
  - The output guard of `mark_unusable` for a replay compares the stored output; it should compare the replies.
  - An interrupted call is recorded as `backend_error` with no error text.

---

## ⚖️ Options Considered {#options}

**Queue**

| Option                                                    | Pros                                                                                                  | Cons                                                                          |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| **Own jobs table in Postgres + SKIP LOCKED** (chosen)     | No extra service. The queue **is** the metrics. Full control over `run_after`, deferral and dedupe.   | We maintain ~200 lines of queue code.                                         |
| Procrastinate (Postgres-based task library)               | Same database, delays and retries built in.                                                           | Its own tables sit next to ours. Metrics still need our own tables.           |
| Redis + RQ / Arq / Celery                                 | Mature.                                                                                               | An extra service. Job history is not durable, so it must be copied for metrics. |
| No queue (`BackgroundTasks` in FastAPI)                   | Simplest.                                                                                             | Work dies with the API. No delays, no deferral.                               |

### Queue as a module, not a container {#queue-placement}

**Decision: the queue mechanism is a shared Python module (`core/queue.py`), not a dedicated container.** The Postgres `jobs` table **is** the queue. The API and the worker both import the module and talk to the database directly:

```text
 api     ──enqueue()──▶  ┌──────────────────────┐
                         │ Postgres: jobs table │  ◀── the queue lives here
 worker  ──claim()────▶  └──────────────────────┘      (SKIP LOCKED, NOTIFY)
         ──complete()─▶
```

- **Receiving a message and storing it:** the API calls `enqueue()`, which is one `INSERT` + `NOTIFY`.
- **Handing a message to the runner:** the worker calls `claim()`, which is one `SELECT … FOR UPDATE SKIP LOCKED`. Postgres guarantees that two workers never get the same job.

| | **Queue module in api/worker** (chosen) | Dedicated queue container |
| --- | --- | --- |
| Moving parts | None extra | +1 container, its own API, health checks and deploy |
| Failure points | Postgres only | Postgres **and** the queue service. If it's down, nothing is enqueued or claimed. |
| Transactions | Enqueuing a job and other DB writes happen in **one transaction** (for example "mark the item ready + enqueue publish") | Lost: an HTTP call to another service can't be part of the database transaction |
| Latency | A direct DB call, `NOTIFY` wake-up | An extra network hop |
| Code | ~200 lines, shared | The same logic plus an HTTP layer around it |

A dedicated queue service or a broker (RabbitMQ and similar) only pays off with **clients in other languages or outside the LAN** that can't reach the database, **very high throughput**, or **many independent services** sharing one queue. None of these apply.

**Remote workers later:** if a worker ever runs where it can't reach Postgres (a cloud worker, a GPU box), the API gets two extra endpoints, `POST /workers/claim` and `POST /jobs/{id}/complete`. They call the same `claim()` and `complete()` functions. The API then acts as the queue's front door for remote workers, while local workers keep using the database directly. Still no new container.

**Swappable:** job handlers only use the `enqueue()` / `claim()` / `complete()` interface, so the implementation can be replaced (Procrastinate, Redis, a broker) without touching them.

**Scheduling:** a loop in the worker (chosen) vs. a cron container (`supercronic`) calling the API vs. a systemd timer running `curl` on the LXC. The loop keeps everything in Compose and can later read schedules from the DB. A timer calling the API still works as an extra trigger.

### Database options {#db-options}

The database is used for the **job queue** and **logging/metrics** now, and probably for **other data we don't know about yet**, such as embeddings for a mini AI chat or "find related ideas", search over summaries, or app data for future React tools.

| Option                                   | Queue                                                                                    | Logging / metrics                                   | Unknown future data                                                                                                                                  | Ops on the H4                                                     | Fit                                                  |
| ---------------------------------------- | ---------------------------------------------------------------------------------------- | --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- | ---------------------------------------------------- |
| **PostgreSQL** (chosen)                  | ✅ `SKIP LOCKED` + `LISTEN/NOTIFY`: instant wake-up, safe with several workers            | ✅ JSONB, strong aggregates, partitioning if logs grow | ✅ Best: JSONB for data without a fixed shape, full-text search, **pgvector** for embeddings, TimescaleDB if metrics ever get big                    | One container, ~50 MB RAM idle. Major upgrades need dump + restore. | **Chosen**                                           |
| SQLite (+ Litestream for backup)         | ⚠️ Works with one worker. No `SKIP LOCKED`, polling instead of notify.                   | ✅ Fine at this volume                               | ⚠️ JSON functions, FTS5 and `sqlite-vec` exist but are less mature. One writer at a time (API + worker share it).                                 | Zero ops, one file                                                | Viable for the MVP, likely outgrown                  |
| MariaDB / MySQL                          | ✅ `SKIP LOCKED` (MySQL 8, MariaDB 10.6+)                                                | ✅                                                   | ⚠️ Weaker JSON, fewer extensions                                                                                                                     | Similar to Postgres                                               | No advantage over Postgres                           |
| Redis (next to a real DB)                | ✅ Excellent queue                                                                       | ❌ Not durable history                               | ❌                                                                                                                                                   | An extra service, and a real DB is still needed                   | Only as an add-on later, if ever                     |
| MongoDB                                  | ⚠️ Possible, not natural                                                                 | ✅                                                   | ✅ Schemaless                                                                                                                                        | Heavier, weaker for aggregate queries                             | No                                                   |
| Supabase (self-hosted) / PocketBase      | Via Postgres / SQLite underneath                                                         | ✅                                                   | ✅                                                                                                                                                   | Supabase is a big stack; PocketBase is its own framework          | Overkill, or it steers the architecture              |

**Why PostgreSQL:**

- **One database for everything.** The queue, the logs and future data share one backup, one connection and **transactions across all of them**. For example, "mark the item published" and "finish the job" happen together or not at all.
- **It grows without a migration to another database:**
  - **JSONB** holds data whose shape we don't know yet;
  - it becomes normal tables once the shape is clear;
  - **pgvector** adds embeddings when the mini AI chat or "related ideas" arrive;
  - **full-text search** covers the summaries.
- **The same everywhere.** The same image runs in Compose on the Mac and in the LXC, which keeps "run locally = run on Proxmox" true.
- **Mature Python tooling:** SQLAlchemy, Alembic, `asyncpg`/`psycopg`.

**The image:** `pgvector/pgvector:pg17` is the official Postgres 17 with the pgvector extension added. The extension does nothing until a migration runs `CREATE EXTENSION vector`, so it costs nothing now and is ready later.

**What to watch:**

- **Log growth:** `job_events` is the table that grows. The retention cleanup (delete events older than N days) is enough. Monthly partitioning is an option much later.
- **Major upgrades** (17 → 18) need a dump and restore. Keep the image pinned to `pg17` and upgrade on purpose.
- **Backups:** a nightly `pg_dump` to a mounted folder, plus the Proxmox vzdump of the LXC.
- **SQLite stays a fallback** if Postgres ever feels too heavy. The queue code sits behind an interface, but moving would mean giving up `NOTIFY`, parallel workers and pgvector.

**Hosting:** an LXC with Docker Compose (chosen: same as local, same as FreeLLMApi) vs. an LXC without Docker (Python, Postgres and systemd installed directly: lighter, but the Mac and Proxmox setups would differ) vs. one LXC per component (more networking and backups for no gain).

---

## ⚠️ Gaps, Flaws & Risks {#risks}

1. **Pages go live only when you deploy.** The service pushes to `epiaku-docs` `main`, but the site changes only when you run `deploy.sh`. Your local checkout also has to `git pull` first, or the next manual deploy overwrites the new pages with an older checkout.
2. **No Hugo build check in the MVP.** A page that passes the Python validation could still break the Hugo build (for example a bad shortcode inside the LLM's body text). You would see it in your manual build. Fix it by re-running the note, or with a revert.
3. **Git races.** Obsidian pushes to `idea-bucket` at any time, and you push to `epiaku-docs` from the Mac. Mitigated by one Git writer, `pull --rebase` + one retry, and pages that only the pipeline writes.
4. **Waiting jobs vs. edited notes.** A document can wait in `output/` for hours. If you edit or re-clip it in Obsidian meanwhile, a new copy lands in `inbox/`. Every inbox file gets its **own** calculated name (the short guid makes it unique), so a re-clip is a **new document**: it does not overwrite the `archive/` or `output/` copy of the old one. Only the page in `epiaku-docs` is replaced, by `id`. A clip that is an earlier snapshot of a longer one (same messages, cut off at the end) is archived and moved to `duplicates/` (with `duplicate_of` in the frontmatter) and gets **no LLM call**; a clip whose content differs is processed on its own, the later page overwrites the earlier one by `id`, and the run warns "same id as an earlier document". In Stage B two almost identical documents can come out when this happens. That is accepted, because it is rare. It is worth a rule (the newer capture supersedes the older open item) only if it shows up often.
5. **Free models on long inputs.** Without map-reduce (F7), FreeLLMApi will fail on long texts. The MVP routes AI chats and YouTube to the `openai` profiles (1M context) for that reason. A note that is unexpectedly long fails without an LLM call (`LLM_MAX_INPUT_CHARS`, default 400,000 characters) and lands in `failed/` with the reason, instead of deferring forever.
6. **API budget.** The keys for the `clippings` and `youtube` profiles have a **budget cap**, so the service can reach it and then cannot call the LLM at all. It is treated as its own condition (not a rate limit): the backend is blocked for the run, one `ERROR` says how many notes are waiting, the documents stall in `output/` (`stage: deferred`), and they are retried every `budget_retry_delay` from Stage B (until then you move them back into `inbox/`). Because there is **no automatic fallback** to another provider, only two things fix it: raise or renew the key's budget, or point the profile at another provider in `profiles.yaml` (or run with `--profile`). Notes on the other providers are unaffected. In the MVP you notice it through the log and `GET /items?status=deferred`; **F: a budget guard** (see [F3](#f-llm)) adds a soft limit that warns at 80% and a notification, so it does not come as a surprise. Watch `tokens_in` and `tokens_out` in the metrics meanwhile.
7. **Stuck is only visible if you look.** In the MVP, stuck notes show up in the log and in `GET /items?status=stuck`. The dashboard (F1/F4) and notifications (F7) make this visible.
8. **Docker in an LXC** can break on Proxmox upgrades. Snapshot before upgrading.
9. **Worker down = quiet metrics.** If the worker is down, no metrics are recorded. `/health` reports the worker's heartbeat, and the future dashboard shows "worker last seen".
10. **The one-call summary can still be wrong.** Schema validation and the free timestamp/tool checks catch some mistakes, not all. There is no second LLM call to catch the rest; if quality is not good enough, revisit a checking step deliberately rather than by default.
12. **Classifying Gemini chats.** A general Gemini chat whose first message happens to contain a YouTube link would be treated as `youtube-gemini`. Use `type: ai-chat` in the clip to override, or tighten the rule (for example, also require the YouTube summary prompt's headings in the answer).
13. **`yt-dlp` breaks when YouTube changes.** Rebuild the image regularly, or upgrade `yt-dlp` at worker start.
14. **Gemini's own mistakes pass through.** The `youtube-gemini` class only restructures Gemini's answer, so a wrong claim is copied faithfully (for example "30% to 40% of sales occur during follow-up" where the video says 30 to 40% *more* sales). Nothing can fix this in our code. Do not rely 100% on a Gemini page: when a number or claim matters, compare it with the direct page of the same video.
15. **LLM output varies between runs.** The same input gives different tags and wording on each run. One run is not a trend. Judge a prompt change on several pages, and use the `output/` history in the test repos (one commit per run) to compare before and after.
16. **The free provider is unstable.** FreeLLMApi often answers 502 or times out (a call can take a minute), and the model behind `auto` changes between calls. In-call retries help, but a run with many notes can still take minutes per note. Do not classify an error by words in its message: a 502 that mentioned a "retry budget" was once reported as an empty wallet.
17. **YouTube facts change over time.** Views and likes move, and chapters can appear on a video days after it was published. The fetch date is stored, and a rerun gives a newer page. YouTube also blocked this machine (429) after heavy live testing on 2026-09-28: keep tests offline.
18. **Tags drift.** An LLM-chosen topic tag is sometimes plausible but off (a RAG page tagged `ai-agents`). Tags are rarely used on the site, so the decision is to do nothing now and look at a few tag pages after about 20 pages.

---

## ❓ Open Questions {#open-questions}

Carried over from Stage A, to settle in Stage B:

1. **The queue design.** The YouTube protections are built in Stage A (saved facts, one paced extraction, a gap between fetches, a breaker, an offline switch). What is left is moving the gate state into Postgres and the queue itself (our own Postgres queue, Procrastinate as the fallback); see [YouTube IP bans and the queue](../idea-catcher-youtube-bans-and-queue-options/).
2. **Two retry layers.** Stage A retries a transient failure inside a call (5 calls, doubling wait), and stage B adds a job-level `retry_delay`. Decide how they add up: a failing provider can now make one note take several minutes before it defers.
3. **Where the business context and the glossary live.** Today both are files inside the code package (`pipeline/epiaku-context.md`, `glossary.yaml`). A running service may want them in the `idea-bucket` repo or the database, so editing one does not need a deploy.
4. **Stuck and deferred notes need to be visible.** In Stage A you find them by looking in `output/` and the log.
5. **A model test suite.** The profiles were chosen by hand and by a few comparisons, not by a repeatable suite.
6. **Name matching.** `--file` and `--requeue` need the whole name. A unique prefix could be accepted.
