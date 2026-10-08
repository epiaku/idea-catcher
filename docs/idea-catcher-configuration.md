---
title: "Idea Catcher: Configuration and Secrets"
linkTitle: "Idea Catcher Configuration"
description: "Which settings the Idea Catcher needs and where each one goes: secrets and machine settings in .env, the three LLM profiles in profiles.yaml, per-run options on the command line."
weight: 30
type: docs
---

This page lists every setting the Idea Catcher (Stage A, the local CLI) reads, and **where to put it**. The flow is described on the [pipeline page](../idea-catcher-pipeline/), the software on the [architecture page](../idea-catcher-service-architecture/).

## Where each kind of setting lives

| Setting                  | Goes in            | In Git? |
| ------------------------ | ------------------ | ------- |
| API keys (secrets)       | `.env`             | **No**  |
| Paths, URLs, log level   | `.env`             | No      |
| Model names              | `.env`             | No      |
| Provider per capture     | `profiles.yaml`    | Yes     |
| Choices for one run      | command-line flags | –       |
| GitHub access            | your Git setup     | –       |
| Prompts, templates, tags | files in `src/`    | Yes     |

- **`.env`** is in the `idea-catcher` repo root and is listed in `.gitignore`. A real environment variable with the same name always wins over `.env`.
- **`profiles.yaml`** is in the repo root. It contains no secrets, so it is in Git.
- **GitHub access** (pull and push) uses your normal SSH key or Git credential helper. The pipeline stores no GitHub token.
- **Prompts, templates and tags** live in `src/catcher/modules/`, for example `prompts/youtube.md` and `pipeline/tags.yaml`.
- **The business context** is `src/catcher/modules/pipeline/epiaku-context.md`, next to `tags.yaml`: a few lines, in your own words, on who Epiaku is (what the company does, the audience, what you sell or plan to, what you are building now, your stack and constraints). It is used for the **Channel Application** part of the **direct YouTube pages only** (class `youtube`), where the LLM has to write that advice itself. It is not used for Gemini pages (`youtube-gemini`): there the LLM only restructures what Gemini wrote, and Gemini's own advice is kept as it is (your Gemini prompt already carries the Epiaku description). Without it the LLM only knows the one sentence in the prompt ("the YouTube channel, Vibe Coding Tech Stack demos, and building phone apps or small SaaS applications") and gives generic ideas. With it, the prompt asks for 2 to 4 concrete suggestions that fit the context, and to say so when a video does not fit Epiaku. Lines between `<!--` and `-->` are for you and are not sent. The text is sent on every direct YouTube summary, so keep it to about 10 lines. The file in the repo is a starter made from what the repo already says about Epiaku: edit it.
- **The glossary** is `src/catcher/modules/pipeline/glossary.yaml`, next to `tags.yaml`: words and names you use often that dictation gets wrong (`epiaku`, `Claude Code`, `VS Code`...). It is used for **dictated notes only** (the `note` prompt). Each line is either just the right spelling, or the right spelling with what dictation often writes instead:

  ```yaml
  terms:
    - Obsidian                                  # just the spelling
    - Claude Code: [cloud code, clod code]      # the spelling, and what it is often heard as
  ```

  The LLM gets the list as a hint: when a word in the note clearly sounds like one of the terms, it writes the term's spelling. It is told to use a term only where the note means it, and never to add one the note does not mention. A missing or empty file just means no glossary section in the prompt. A long list costs tokens on every note (about 10 per term), so keep it to the words that really go wrong.

**Rule of thumb:** anything that would be dangerous to publish goes in `.env`. Anything that decides behaviour and is fine to share goes in `profiles.yaml` or the code.

## First-time setup

```bash
cp .env.example .env      # then edit .env
```

Run the commands from the repo root, because `.env` and `profiles.yaml` are found from there.

- **Notes only (free):** set `IDEAS_REPO`, `DOCS_REPO` and `FREELLMAPI_URL`.
- **Also AI chats and YouTube:** add `OPENAI_API_KEY` and the two `OPENAI_MODEL_*` names.

## `.env`: every variable

### Repos and files

- `IDEAS_REPO` (default `../idea-bucket`): path to your local `idea-bucket` checkout.
- `DOCS_REPO` (default `../epiaku-docs`): path to your local `epiaku-docs` checkout. Pages are written under `hugo/content/en/docs/idea-bucket/`.
- `PROFILES_FILE` (default `profiles.yaml`): which profiles file to use.

### FreeLLMApi (the `notes` profile)

- `FREELLMAPI_URL` (default `http://localhost:3001/v1`): base URL of your FreeLLMApi proxy.
- `FREELLMAPI_MODEL` (default `auto`): model for notes. Pin a model here later instead of `auto`.
- `FREELLMAPI_API_KEY` (default `not-needed`): **secret**, only if your proxy asks for a key. The default works for an open proxy on your LAN.

### OpenAI (the `clippings` and `youtube` profiles)

- `OPENAI_API_KEY` (default empty): **secret**. Needed for AI chats and YouTube. Without it those notes are deferred with `OPENAI_API_KEY is not set`.
- `OPENAI_BASE_URL` (default `https://api.openai.com/v1`): only change this to use another OpenAI-compatible service.
- `OPENAI_MODEL_CLIPPINGS`: model for AI chats. The model ids are listed in `.env.example`.
- `OPENAI_MODEL_YOUTUBE`: model for both YouTube classes.

If an `OPENAI_MODEL_*` is empty, the note is **deferred with a configuration `ERROR`**. It is not moved to `failed/`.

### Other

- `ARTIFACT_MAX_MB` (default `25`): files in `inbox/` that are not markdown (PDFs, images) are copied to epiaku-docs. Files over this size are skipped with a warning and stay in `inbox/`.
- `LLM_TIMEOUT_S` (default `600`): seconds to wait for one LLM answer before it counts as unavailable.
- `LLM_MAX_ATTEMPTS` (default `5`): how many calls in all a request gets when a call fails in a way that may pass next time (a 5xx such as a 502 or 503, a timeout, a dropped connection). `1` turns retrying off. A used-up budget, a rate limit (429), a bad key and other 4xx errors are not retried. After the last attempt the note is `deferred`, as before. FreeLLMApi picks a provider for each call, so a retry is often routed to one that works.
- `LLM_MAX_INPUT_CHARS` (default `400000`): a document (with its YouTube facts) longer than this is **failed** without an LLM call, and so is an empty one. A model that answers "context length exceeded" fails the document too, instead of being retried at every run.
- `LLM_RETRY_WAIT_S` (default `2`): seconds to wait before the second attempt. The wait doubles before each one after it (2, 4, 8, 16 s with the defaults). Each failed call can itself take up to a minute, so five failures in a row take around five minutes.
- `LLM_TRACE` (default `true`): every LLM call of `run pipeline` leaves a trace (the replies, tokens, outcome) in `llm/` of `idea-bucket`. See [Saved LLM replies](../idea-catcher-how-to-run/#saved-llm-replies).
- `LLM_TRACE_PROMPT` (default `false`): also save the full prompt in each trace. Large, because it holds the whole document; without it a trace keeps only the prompt's `prompt_sha256`.
- `LLM_CACHE` (default `true`): a run reads a saved good reply in `llm/` (same task, profile, prompt version and document text) instead of calling the model. `false` always calls it. `--refresh-llm` skips the saved reply for one run.
- `LLM_BLOCK_S` (default `600`, 10 minutes; must be more than 0): the worker only. The cool-down starts after any "backend unavailable" error of an `llm.reason` job: the backend is down (connection refused, a timeout, a 5xx), hit a rate or usage limit, refused the request (a wrong key, another 4xx), or its API key is missing. That backend is then not called for this many seconds: the next documents that need it are `deferred` without a call (their reason says until when). A model the backend does not know (a misspelled `OPENAI_MODEL_CLIPPINGS`) blocks only that **profile** for this long, so the other profiles of the same backend still run. That is recognised only for an HTTP 400 or 404 whose error code or text says the model is unknown (`model_not_found`, "The model ... does not exist"); any other refused request, for example a 400 because the model does not accept the JSON reply format, blocks the whole backend. A document whose reply is saved in `llm/` is still served. When the time is over, the next document tries once; if it fails again, the block starts again. The blocks are rows in the Postgres `resources` table (`openai`, or `openai:clippings` for one profile), shared by every worker and kept over a restart. Stage A's `run pipeline` keeps its own rule: a backend that hit a usage limit or budget is not called again in that run.
- `LLM_BUDGET_BLOCK_S` (default `21600`, 6 hours; must be more than 0): the worker only. Like `LLM_BLOCK_S`, for a backend whose API key's budget is used up: raise or renew the budget, and the first document after this time goes through.
- `STUCK_AFTER_DAYS` (default `3`; must be more than 0, fractions allowed): the worker only. An item that has been `deferred` for this many days becomes `stuck` (a warning event; the worker checks at every reap). It is still retried: a `pipeline.run` job with `retry_deferred` picks up `deferred` and `stuck` items, a retry that defers again leaves it `stuck` with its clock, and a publish ends it. `catcher items list --status stuck` shows them.
- **YouTube** (to avoid an IP ban; see [YouTube and the gap between calls](../idea-catcher-how-to-run/#youtube-gap)):
  - `YOUTUBE_REQUEST_DELAY_S` (default `10`): seconds between the requests inside one fetch (yt-dlp `sleep_interval_requests`).
  - `YOUTUBE_MIN_GAP_S` (default `120`, 2 minutes): minimum seconds between the start of two fetches. `YOUTUBE_GAP_JITTER_S` (default `300`) adds up to that many random seconds, so the real gap is 2 to 7 minutes. It started at 10 minutes in the design and was lowered on 2026-10-02 to start low and watch for a block; raise it if YouTube blocks the IP.
  - `YOUTUBE_BLOCK_HOURS` (default `6`; more than 0 and at most 24, anything else stops the program with a validation error): no calls for this long after a block. It doubles each time, up to 24 hours. `0` would turn the breaker off, and a value above 24 would be read back by the gate as damage.
  - `YOUTUBE_OFFLINE` (default off): `1` means never call YouTube; saved facts still work.
  - `YOUTUBE_SKIP_MANIFESTS` (default off): skip yt-dlp's request for the video formats, which we never use. **Untested live:** try one `youtube facts` by hand before turning it on.
  - `YOUTUBE_WAIT_MAX_S` (default `1800`): with `--wait-youtube`, the longest sleep for the gap inside a run.
  - `YOUTUBE_NEGATIVE_TTL_H` (default `24`): a video without captions is asked about again only after this many hours.
  - The gate state (the gap and a block) lives in Postgres, in the `resources` row `youtube` of `DATABASE_URL` (see [The YouTube gate](../idea-catcher-how-to-run-stage-b/#youtube-gate)). The worker, `run pipeline` and `youtube facts` share it, and the settings above apply to all of them. `CATCHER_STATE_DIR` is gone (2026-10-04): an old line in `.env` is ignored.
- Backfill (`catcher youtube import`):
  - `BACKFILL_PRIORITY` (default `-10`): the priority of every job of a released backfill video (a clip note with `backfill: true`). New clips run at `0`, so they are claimed first; the YouTube gate paces backfill fetches like any other.
  - `BACKFILL_DAILY_LIMIT` (default `10`, 0 or more): the cap per rolling 24 hours that `--release` uses when no `--limit` is given. Start small (5, then 20, then 50) and raise `YOUTUBE_MIN_GAP_S` before raising it.
- `TRANSCRIPT_LANGUAGES` (default `en`): preferred YouTube transcript languages, comma-separated, for example `en, nl`.
- `GIT_AUTHOR_NAME` (default `idea-catcher`): author name on the commits the pipeline makes.
- `GIT_AUTHOR_EMAIL` (default `idea-catcher@users.noreply.github.com`): author email on those commits.
- `DATABASE_URL` (default `postgresql+psycopg://catcher:catcher@localhost:5432/catcher`): the Postgres database: the queue, the state, the YouTube gate and the one-at-a-time lock. **Required** for `run pipeline` (also `--dry-run`), `render`, `youtube facts`, `youtube gate`, the worker, the `jobs` and the `db` commands: without it they stop with exit code 2. Only `scan` and `reason` (which refuses YouTube clips) work without it. The run lock and the YouTube gate are per database: two different `DATABASE_URL`s on one set of checkouts mean two writers on one checkout and two gates, so use one database for a set of checkouts. The default matches the development container in [Database](../idea-catcher-how-to-run-stage-b/#database). It holds a password, so a real URL goes in `.env`.
- `LOG_LEVEL` (default `INFO`): `DEBUG`, `INFO`, `WARNING` or `ERROR`. The flag `--log-level` overrides it for one run.
- `LOG_FILE` (default none): also write the log to this file. The folder is created.

## `profiles.yaml`: the three profiles

A **profile** says which API provider and model a kind of capture uses.

```yaml
default: notes
profiles:
  notes:     { backend: freellmapi, model: "${FREELLMAPI_MODEL:-auto}" }
  clippings: { backend: openai,     model: "${OPENAI_MODEL_CLIPPINGS}" }
  youtube:   { backend: openai,     model: "${OPENAI_MODEL_YOUTUBE}" }
  fake:      { backend: fake }
```

### What each profile is for

- **`notes`**: short dictated notes. Provider: FreeLLMApi. Uses `FREELLMAPI_URL`, `FREELLMAPI_MODEL` and `FREELLMAPI_API_KEY`.
- **`clippings`**: AI chats (Gemini and Claude clips that are not YouTube) **and web clips** (articles and other pages). Provider: OpenAI. Uses `OPENAI_API_KEY` and `OPENAI_MODEL_CLIPPINGS`.
- **`youtube`**: direct YouTube clips (`youtube`) and Gemini video-summary chats (`youtube-gemini`, reformatted only — no YouTube API call for that class). Provider: OpenAI. Uses `OPENAI_API_KEY` and `OPENAI_MODEL_YOUTUBE`.
- **`fake`**: returns canned JSON and calls nothing. Use it for free dry runs and tests.

### How it works

- **`backend`** is `freellmapi`, `openai` or `fake`. The two real backends speak the same OpenAI-style protocol. They differ only in base URL, key and model.
- **`model`** may use `${NAME}` or `${NAME:-default}`. It is filled from the environment, so from `.env`.
- **To change the provider** of one kind of capture, edit its line. Chats and videos can use different models because they are separate profiles.
- **The document class picks the default profile:** `note` uses `notes`, `ai-chat` and `web-clip` use `clippings`, and the YouTube classes use `youtube`. The flag `--profile` overrides it for a whole run.
- **A profile is not a folder.** The profile `clippings` has nothing to do with where a page goes: the epiaku-docs folders are `notes/`, `youtube/` and `web-clips/` (an AI chat goes to `web-clips/`), set by the document's `destination` field. See [Where a page goes](../idea-catcher-pipeline/#destination).
- **Unknown fields are rejected.** An old file with `when:` or `backend: claude-code` fails loudly.
- **There is no fallback between providers.** If a provider is down or its budget is used up, its documents stall in `output/`. Change the profile's provider, or raise the budget, to continue.

The lines `retry_delay`, `budget_retry_delay` and `stuck_after_days` in the file are **not used in Stage A**. They matter from Stage B, when Postgres and the queue arrive.

## Options for one run (command line)

- **`--ideas PATH`** (`scan`, `run pipeline`): use this `idea-bucket` checkout instead of `IDEAS_REPO`. Handy for a run on copies.
- **`--docs PATH`** (`run pipeline`, `render`): use this `epiaku-docs` checkout instead of `DOCS_REPO`.
- **`--profile NAME`** (`run pipeline`, `render`, `reason`): use this profile for every note. `--profile fake` costs nothing.
- **`--limit N`** (`run pipeline`): process at most N notes. Use it to cap spend.
- **`--file NAME`** (`run pipeline`, `scan`): process only the named document from `inbox/`, for example `--file "New chat"`. Repeat it for more. A name that matches nothing gives a warning. See [How to run](../idea-catcher-how-to-run/).
- **`--dry-run`** (`run pipeline`): change no files and commit nothing. **With a real profile it still calls the LLM, unless a good reply is saved (it reads those, free).**
- **`--push`** (`run pipeline`): push both repos after committing. **Off by default.**
- **`--log-level LEVEL`** (any command): log level for this run. Put it before the command name: `uv run catcher --log-level DEBUG run pipeline`.

## Secrets: what to keep safe

- **Secrets in this project:** `OPENAI_API_KEY`, and `FREELLMAPI_API_KEY` if your proxy uses one. Nothing else in `.env` is sensitive.
- **Never commit `.env`.** It is already in `.gitignore`. `.env.example` has no secrets and is the only file to commit.
- **The key is never logged.** It is never written to a page, a sidecar file or an `.error.txt`. A bad key shows as `authentication failed: check OPENAI_API_KEY`.
- **The key has a budget cap.** When it is used up, the run stops calling that provider, logs one `ERROR` (`openai budget reached: N note(s) waiting`), and the documents stall in `output/`. Pick a budget with some headroom, and raise it when you see that line.
- **Use a separate key for testing**, with a tiny budget, so a mistake cannot use up the real one.
- **Rotate a leaked key** in the provider's console, then update `.env`. Nothing else needs to change.

## Check your setup without spending tokens

```bash
uv run catcher run pipeline --profile fake --dry-run
uv run catcher --log-level DEBUG scan
```

The first reads both repos and calls nothing. The second lists what is in the inbox and what a run would do with it.

A real check costs a little and is yours to run:

```bash
uv run catcher reason "<a document in inbox/>" --profile clippings
```

It should print validated JSON and a `backend=openai` line.

## Coming in later stages

These do not exist yet in Stage A. They are listed so you know where they will go.

- **Stage B, Postgres and queue:** the database URL and password go in `.env` (secret). `DATABASE_URL` exists since B1 (see above). The retry settings go in `profiles.yaml`, where they already are.
- **Stage C, API:** API keys for the callers (`Authorization: Bearer …`), one per device, as `name:scopes:key` in `.env` (secret).
- **Proxmox:** the GitHub fine-grained token, `OPENAI_API_KEY` and the schedule go in `.env` on the LXC, not in Git.
- **Future:** a monthly budget guard per provider, with a warning at 80%, in `profiles.yaml`.
