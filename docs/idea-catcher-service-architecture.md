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
| R1  | **Scheduled runs** a few times a day: look only at `idea-bucket/inbox/` for work. When work on a document starts, move it out of `inbox/`: an untouched copy to `archive/` and a working copy to `output/`, which becomes the final page. Write pages to `epiaku-docs` under `hugo/content/en/docs/idea-bucket/<type>/`. | MVP    | A scheduler plus a `pipeline.run` job. Git access to both repos.                                   |
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
| Folders             | **Five top-level folders, one rule:** a run **only looks at `inbox/`** for work. When work on a document starts, it leaves `inbox/`: it gets a **calculated file name** (`YYYYMMDD-<short guid>-<title>.md`, at most 128 characters), the original goes to `archive/` (unchanged except for two added frontmatter lines, `original_filename` and `calculated_filename`) and a working copy to `output/` (with a `stage` in its frontmatter), so a document is never started twice. `output/` then holds the final page. `failed/` holds permanent failures (with an `.error.txt`) and `duplicates/` earlier snapshots of a longer clip. A temporary error stalls the working copy in `output/` (`stage: deferred`, with the reason). **To retry, move the file from `archive/` back into `inbox/`**: it keeps its calculated name, so the next run overwrites the stalled copy. The same name is used in `archive/`, `output/` and `epiaku-docs`, so two documents called `New chat.md` never overwrite each other. A run never reads `output/`. Each folder has `notes/` and `clippings/`, and a file keeps its inbox name. See [the layout](../idea-catcher-pipeline/#repo-layout). |
| Failures            | **No fallback between providers.** A failed LLM job is logged and **retried on the next run** (after `retry_delay`). After **3 failed days** the note is flagged **stuck** in the log and metrics, and keeps retrying. |
| Same video twice    | A video clipped directly **and** summarized in Gemini keeps **both** pages (IDs `<video-id>` and `<video-id>-gemini`), so they can be compared.                                           |
| One call per class  | Every class (including both YouTube classes) makes exactly **one** LLM call, on its own prompt file. No separate review step and no second call for any class. |
| Queue               | The Postgres `jobs` table **is** the queue, used through a shared module (`enqueue` / `claim` / `complete`) in the API and the worker. **No dedicated queue container** ([why](#queue-placement)). |
| Database            | **PostgreSQL 17** (image `pgvector/pgvector:pg17`) for the queue, logging, metrics and future data. See [Database options](#db-options) for the alternatives.                          |
| Hosting             | **One Proxmox LXC with Docker Compose.** The same `compose.yaml` runs locally. No VM.                                                                                                     |
| Service deploy      | **Manual**, with a `deploy.sh` script in the service repo.                                                                                                                                |
| Docs site deploy    | **Unchanged.** The service commits pages to `epiaku-docs` `main`. You deploy the site manually with `deploy.sh`, as today.                                                               |
| Hugo build check    | **Not in the MVP.** Python validates the frontmatter and page structure. Your manual Hugo build catches anything else.                                                                   |
| Repo                | A new **`idea-catcher`** repo for the service (and later its React front end).                                                                                                          |
| Retry               | A deferred LLM job is retried after **`retry_delay`** (default 60 minutes). If the API key's **budget is used up**, it waits **`budget_retry_delay`** (default 6 hours) instead. There is no evening window: every profile is an API and can run at any hour. |
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
| YouTube         | Facts (counts, transcript) for **clips** in the inbox, then the LLM step                  | Endpoint, cache, extra verify checks, Gemini for videos without captions                                |
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
- **Claim:** the worker runs `SELECT … WHERE status='queued' AND run_after <= now() ORDER BY priority, run_after FOR UPDATE SKIP LOCKED LIMIT 1`. `SKIP LOCKED` keeps this correct when more workers are added later.
- **Wake-up:** the API sends `NOTIFY jobs` after inserting, and the worker `LISTEN`s, so an API-triggered run starts within a second. A 30-second poll picks up delayed jobs whose time has come.
- **One pipeline run at a time.** A second trigger while a `pipeline.run` is queued or running returns the **existing** `job_id`. This is enforced with a partial unique index on `(type) WHERE status IN ('queued','running')`.
- **Heartbeat:** a running job updates `heartbeat_at` every ~15 s. On start, the worker resets jobs with an old heartbeat (it crashed mid-job) to `queued`. Every step is idempotent (overwrite by ID), so re-running is safe.
- **Progress:** handlers write `job_events` (level, message) and update `progress_done/total`. Clients **poll** `GET /jobs/{id}`. A live stream is a [future feature](#f-frontend).

### 🧠 LLM Step & Profiles {#mvp-llm}

All reasoning goes through **one function** and **one job type**. A module never calls an LLM client directly.

```python
# catcher/modules/llm/service.py
class LlmOptions(BaseModel):
    profile: str | None = None  # a named profile from profiles.yaml: "notes", "clippings", "youtube"
    backend: Literal["freellmapi", "openai", "fake"] | None = None
    model: str | None = None  # e.g. an OpenAI model id, or a FreeLLMApi model id


class LlmRequest(BaseModel):
    task: str  # prompt template: "note", "ai-chat", "youtube-summary"
    prompt_version: str  # stored with the result, so replays are traceable
    input: dict  # template variables (note body, transcript, facts, …)
    schema_name: str  # Pydantic output model, e.g. "NoteSummary"
    llm: LlmOptions


async def reason(req: LlmRequest) -> LlmResult:
    """Build the prompt, call the backend, validate the JSON, return the result + usage."""
```

The same function handles notes, AI chats and YouTube clips now, and the YouTube endpoint, web clips or anything else later. A new task means a new prompt template and a new output schema, not new LLM code.

**Where the options come from** (the first one that sets a field wins):

1. The `llm` block in the **job message** (for example `POST /pipeline/runs {"llm": {"profile": "notes"}}`).
2. The **default profile of the document class** (`doctypes.py`).
3. The **global default** in `profiles.yaml`.

**MVP profiles** (`profiles.yaml`). Three profiles, one per kind of capture, each an API with a key. We start with these and adjust after testing them with the [model test suite](../idea-catcher-pipeline/#test-suite).

| Profile     | Backend      | Model                                        | Default for                                                       |
| ----------- | ------------ | -------------------------------------------- | ----------------------------------------------------------------- |
| `notes`     | `freellmapi` | `$FREELLMAPI_MODEL` (starts as `auto`)       | **Notes** (`note`)                                                |
| `clippings` | `openai`     | `$OPENAI_MODEL_CLIPPINGS` (e.g. `gpt-6-sol`) | **AI chats** (`ai-chat`: Gemini and Claude clips that are not YouTube) and **web clips** (`web-clip`: any other clipped page) |
| `youtube`   | `openai`     | `$OPENAI_MODEL_YOUTUBE` (e.g. `gpt-6-sol`)   | **`youtube` and `youtube-gemini`**                                |
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
| Timeout, connection error, server error, context too long | Log a **warning** event with the reason. The job becomes `deferred` with `run_after = now + retry_delay`.                                   |
| Rate limit (`429`, `rate_limit_exceeded`)               | Temporary. `deferred` with `run_after = now + retry_delay`. The worker **stops claiming other jobs of the same backend** until then (they would hit the limit too); other backends carry on. |
| **Budget reached** (`insufficient_quota`, billing or spend limit) | The API key's budget is used up, so the backend cannot answer **until the budget is raised or renewed**. The job is `deferred` with `run_after = now + budget_retry_delay` (default 6 hours, so we notice a raised budget the same day without hammering the API). The backend is **blocked for the rest of the run**, and the log gets **one `ERROR` per backend** with the number of waiting notes: `openai budget reached: 4 note(s) waiting; raise the key's budget or point the profile at another provider`. Nothing is lost: the notes stay in `output/` and go through as soon as calls work again. |
| Failing on **3 days** (`stuck_after_days`)              | The note's item is flagged **`stuck`**, with a warning event. It **keeps retrying**. `GET /items?status=stuck` lists these notes, so you can pick another profile with `catcher items requeue <doc_id> --profile …`. |

The working copy stays in `output/` (`stage: deferred`, with the reason) and the untouched original is in `archive/`, so nothing is lost. In Stage A you retry by moving the file back into `inbox/`.

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
| `youtube-gemini` | a Gemini chat that summarized a video              | `youtube-gemini.md`, given Gemini's answer plus Python's transcript and facts (checks and reformats in one call) | `youtube` (OpenAI) | 1         |

This replaces an earlier design where `youtube` got a second, separate "reviewer" LLM call that re-checked a summary already built from the real transcript — extra cost with no new source of truth. `youtube-gemini` always needed only one call (Gemini's answer had to be checked against our transcript somehow), so it is unchanged in spirit, just renamed to its own prompt file instead of sharing a generic `review` task.

**Free checks stay, without a second LLM call.** For either YouTube class, Python checks the finished summary against the facts it was given — a timestamp later than the end of the video, or a tool named that isn't in the transcript, title or description — and reports them as warnings in the page's frontmatter. Nothing is "fixed" by a second model call; the warning just tells you where to look. Views, likes, subscribers and dates always come from `yt-dlp`, never from the model.

**Two YouTube classes:**

| Class            | Comes from                                                                                              | Steps                                                                                                                                                                                             |
| ---------------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `youtube`        | A YouTube link clipped in Obsidian                                                                      | Ingest fetches the facts + transcript → `llm.reason` (`youtube.md`) writes the summary → publish                                                                                                  |
| `youtube-gemini` | A **Gemini web chat** that ran the YouTube summary prompt, clipped in Obsidian (`source` is gemini.google.com and the **first user message contains a YouTube URL**, or an explicit `type: youtube-gemini`) | Ingest extracts the video URL and Gemini's final answer, and fetches the facts + transcript → `llm.reason` (`youtube-gemini.md`) checks Gemini's answer **and** returns it in the `YoutubeSummary` schema → publish. |

Both classes write to `idea-bucket/youtube/`, and **both pages are kept** when the same video arrives both ways. The page ID is the video ID for `youtube` and `<video-id>-gemini` for `youtube-gemini`. This rarely happens, and when it does the two summaries can be compared side by side. Each page links to the other when both exist.

### 🔄 Processing Flow {#mvp-flow}

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
      • youtube / youtube-gemini → yt-dlp counts + transcript (with fetch date)
      • youtube-gemini → also extract the video URL and Gemini's answer
      • no transcript → mark "no transcript", use title + description
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
      (youtube: transcript + facts in; youtube-gemini: Gemini's answer +
      transcript + facts in, checked and reformatted in this one call)
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
- **One Git writer:** only the worker touches the repos, and it runs one job at a time. The phone may still push to idea-bucket during a run, so use `pull --rebase` before pushing and retry once.
- **Per-note errors don't fail the run.** A bad note is marked `failed` (moved to `failed/`) or `deferred` (stalls in `output/` with `stage: deferred`), and the other notes go on.
- **Python validation instead of a Hugo build:** the rendered page must parse as YAML frontmatter + markdown, contain `title`, `description`, `weight` and `type: docs`, only use tags from the allowed list, and only use known shortcodes (`youtube-lite`).
- **Unknown tags** from the LLM are dropped from the page and logged as a `tag_suggestion` event.

The document-type registry, the prompt templates, the output schemas and the Jinja page templates are described in the [Implementation Reference](../idea-catcher-pipeline/#implementation) on the pipeline page.

### ⏰ Scheduling {#mvp-scheduling}

The scheduler runs as a loop inside the worker and **only enqueues jobs**. In the MVP the schedule lives in `.env`:

```bash
SCHEDULE_PIPELINE_RUN="0 8,12,17,21 * * *"   # local time, cron syntax (croniter)
FREELLMAPI_URL="http://<h4-ip>:3001/v1"
FREELLMAPI_MODEL="auto"                      # later: a specific model or a model group/chain
```

Every 30 s the loop checks whether a schedule is due, and enqueues a `pipeline.run` (the dedupe rule applies). After downtime it runs a missed schedule **once**, not once per missed slot. Every capture is published within one run, because every LLM profile is an API that can be called at any hour.

### 🗄️ Database & Metrics {#mvp-database}

**PostgreSQL 17**, with migrations in **Alembic** (why Postgres: see [Database options](#db-options)). Three tables are enough for the MVP:

```text
jobs        id (uuid), root_id, type, queue, status, trigger, api_key_name,
            params jsonb, llm jsonb, result jsonb, error, priority, attempts,
            run_after, progress_done, progress_total, progress_message,
            created_at, started_at, finished_at, heartbeat_at
job_items   id, root_id → jobs (the pipeline.run that first saw it), doc_id, doc_class,
            inbox_path, original_filename, calculated_filename,   (the calculated name and subfolder are
            archive_path, output_path, failed_path, docs_page     the same in every folder),
            status (analyzed / waiting_llm / ready / published / deferred / stuck / failed / duplicate),
            failed_days, warnings jsonb, error,
            llm_profile, llm_backend, llm_model, prompt_version,
            tokens_in, tokens_out, llm_duration_ms, llm_result jsonb,
            created_at, updated_at
job_events  id, job_id, root_id, doc_id, ts, level (info / warning / error), message, data jsonb
```

This already answers the metrics questions, even before a dashboard exists:

| Question                                     | Query                                                                     |
| -------------------------------------------- | ------------------------------------------------------------------------- |
| When did each run happen, and how did it end? | `jobs WHERE type='pipeline.run'`                                         |
| How many docs of each type per run or per day? | `job_items GROUP BY root_id / day, doc_class, status`                   |
| Warnings and errors?                         | `job_events WHERE level IN ('warning','error')`, `job_items.status`       |
| Which model, how many tokens?                | `job_items.llm_backend, llm_model, tokens_*`                              |
| What is waiting or stuck?                    | `job_items WHERE status IN ('waiting_llm','deferred','stuck')`            |

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

The image holds Python 3.12 + uv, Git, `yt-dlp` + Deno, `youtube-transcript-api`. There is **no Node, no Claude Code CLI and no Hugo** in the image, since the docs site deploy stays manual.

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
│       ├── pipeline/            ← doctypes.py, templates/ (Jinja), jobs.py, router.py, tags.yaml
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

**Quick iteration without Docker or a database** (the [stage A](#mvp-stage-a) way of working, which keeps working later): `--no-db` calls the pipeline functions directly instead of going through the queue.

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
- **Test data from the real repos:** `tests/data/` holds a committed copy of only the folders the Idea Catcher uses (`idea-bucket/inbox/` and the Hugo `idea-bucket` pages of `epiaku-docs`). `catcher testdata reset` turns it into fresh git repos without a remote in `tmp/ic`, for manual tries, and the test suite runs the whole pipeline on the same data.
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
| Manually: weekly, or before a bigger change | The live tests (layer 7), which call the real LLM APIs | `uv run pytest -m live` | – |
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
| A7   | The `youtube-gemini` class: its own prompt checks Gemini's answer against the fetched transcript and facts in one call | Process a pipeline-made and a Gemini-made summary of the same video (both pages are kept)                         |
| A8   | One command for the whole flow: `catcher run pipeline [--dry-run] [--push]`                                            | A full run on a copy, then a real run                                                                              |

**Done when:** a real run turns every doc class in the inbox into correct pages on GitHub, and we are happy with the prompts, templates and code structure. Nothing is stored about a document's state in this stage (no Postgres): the folder a file is in, its `stage` and the Python log show what happened, and a deferred document stalls in `output/`. Retry, deferral and metrics come in stage B.

#### Stage B: Postgres + the queue, locally {#mvp-stage-b}

**Goal:** the same work, now driven by jobs in a Postgres queue, with deferral, stuck rules and metrics. Still local, without an API.

| Step | What we build                                                                                                                     | How we test it                                                                                                   |
| ---- | --------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| B1   | `compose.yaml` with only `db` (`pgvector/pgvector:pg17`), SQLAlchemy, Alembic, the `jobs` / `job_items` / `job_events` tables      | `docker compose up db`, `catcher db upgrade`, then look at the tables                                           |
| B2   | The queue module: `enqueue()`, `claim()` (`SKIP LOCKED`, `run_after`), `complete()`, heartbeat, `LISTEN/NOTIFY`                    | Unit tests on the queue. `catcher jobs add demo.sleep` + `catcher worker`, including two workers at once        |
| B3   | Job handlers that call the stage A functions: `pipeline.run`, `llm.reason`, `pipeline.publish`                        | `catcher jobs add pipeline.run` + `catcher worker` on a copy of the repos gives the same pages as stage A       |
| B4   | Deferral (`retry_delay`), `stuck` after 3 days, per-backend blocking on a quota error, metrics on `job_items`                                              | Set `run_after` and the clock in tests. Stop FreeLLMApi and check that jobs defer. Query the metrics with SQL.  |
| B5   | The scheduler loop in the worker, and the worker in Compose next to `db`                                                         | `docker compose up`, then watch scheduled runs happen                                                           |

**Done when:** jobs added by hand or by the schedule process the inbox exactly like stage A, failures defer and recover, and the metrics tables answer the questions in [Database & Metrics](#mvp-database).

#### Stage C: the API, locally {#mvp-stage-c}

**Goal:** start runs and read the results over HTTP.

| Step | What we build                                                                                        | How we test it                                                                                |
| ---- | ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| C1   | FastAPI app with `/health`, API keys + scopes from `.env`                                             | Swagger UI at `http://localhost:8000/docs`. A request without a key must fail.                |
| C2   | `POST /pipeline/runs` (with dedupe) and `GET /jobs/{id}`                                              | Start a run with `curl`, poll the job until it's done                                         |
| C3   | `GET /jobs` and `GET /items` (filters for class, status, `stuck`)                                     | Compare the answers with direct SQL queries                                                   |
| C4   | The `api` container in Compose                                                                        | `docker compose up`: the full stack (`db`, `api`, `worker`) on the Mac                        |

**Done when:** the whole MVP runs locally with `docker compose up`, and everything can be started and checked through the API.

#### Then: Proxmox {#mvp-stage-deploy}

1. Create the LXC (Docker, `nesting=1`, `keyctl=1`), write `deploy.sh`, and put `.env` on the LXC with the GitHub PAT and `OPENAI_API_KEY`.
2. Deploy, and run with the schedule against the real repos.
3. Run the [model test suite](../idea-catcher-pipeline/#test-suite) against the profiles and adjust them.

After that, the [future features](#future) are added one by one in the same way.

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
4. **Waiting jobs vs. edited notes.** A document can wait in `output/` for hours. If you edit or re-clip it in Obsidian meanwhile, a new copy lands in `inbox/`. With the same file name, the next run overwrites the `archive/` copy and the working copy in `output/`, and **updates** the queued LLM job's input instead of adding a second one. With a different file name but the same `id`, a clip that is an earlier snapshot of a longer one (same messages, cut off at the end) is archived and moved to `duplicates/` (with `duplicate_of` in the frontmatter) and gets **no LLM job**; a clip whose content differs is processed on its own, and the later page overwrites the earlier one by `id`.
5. **Free models on long inputs.** Without map-reduce (F7), FreeLLMApi will fail on long texts. The MVP routes AI chats and YouTube to the `openai` profiles (1M context) for that reason. A note that is unexpectedly long will simply defer, and then become stuck.
6. **API budget.** The keys for the `clippings` and `youtube` profiles have a **budget cap**, so the service can reach it and then cannot call the LLM at all. It is treated as its own condition (not a rate limit): the backend is blocked for the run, one `ERROR` says how many notes are waiting, the documents stall in `output/` (`stage: deferred`), and they are retried every `budget_retry_delay` from Stage B (until then you move them back into `inbox/`). Because there is **no automatic fallback** to another provider, only two things fix it: raise or renew the key's budget, or point the profile at another provider in `profiles.yaml` (or run with `--profile`). Notes on the other providers are unaffected. In the MVP you notice it through the log and `GET /items?status=deferred`; **F: a budget guard** (see [F3](#f-llm)) adds a soft limit that warns at 80% and a notification, so it does not come as a surprise. Watch `tokens_in` and `tokens_out` in the metrics meanwhile.
7. **Stuck is only visible if you look.** In the MVP, stuck notes show up in the log and in `GET /items?status=stuck`. The dashboard (F1/F4) and notifications (F7) make this visible.
8. **Docker in an LXC** can break on Proxmox upgrades. Snapshot before upgrading.
9. **Worker down = quiet metrics.** If the worker is down, no metrics are recorded. `/health` reports the worker's heartbeat, and the future dashboard shows "worker last seen".
10. **The one-call summary can still be wrong.** Schema validation and the free timestamp/tool checks catch some mistakes, not all. There is no second LLM call to catch the rest; if quality is not good enough, revisit a checking step deliberately rather than by default.
12. **Classifying Gemini chats.** A general Gemini chat whose first message happens to contain a YouTube link would be treated as `youtube-gemini`. Use `type: ai-chat` in the clip to override, or tighten the rule (for example, also require the YouTube summary prompt's headings in the answer).
13. **`yt-dlp` breaks when YouTube changes.** Rebuild the image regularly, or upgrade `yt-dlp` at worker start.

---

## ❓ Open Questions {#open-questions}

No open questions at the moment. New ones will be added here as the MVP is built.
