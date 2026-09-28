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
- `OPENAI_MODEL_YOUTUBE`: model for both YouTube classes **and the reviewer**.

If an `OPENAI_MODEL_*` is empty, the note is **deferred with a configuration `ERROR`**. It is not moved to `failed/`.

### Other

- `ARTIFACT_MAX_MB` (default `25`): files in `inbox/` that are not markdown (PDFs, images) are copied to epiaku-docs. Files over this size are skipped with a warning and stay in `inbox/`.
- `LLM_TIMEOUT_S` (default `600`): seconds to wait for one LLM answer before it counts as unavailable.
- `TRANSCRIPT_LANGUAGES` (default `en`): preferred YouTube transcript languages, comma-separated, for example `en, nl`.
- `GIT_AUTHOR_NAME` (default `idea-catcher`): author name on the commits the pipeline makes.
- `GIT_AUTHOR_EMAIL` (default `idea-catcher@users.noreply.github.com`): author email on those commits.
- `LOG_LEVEL` (default `INFO`): `DEBUG`, `INFO`, `WARNING` or `ERROR`. The flag `--log-level` overrides it for one run.
- `LOG_FILE` (default none): also write the log to this file. The folder is created.

## `profiles.yaml`: the three profiles

A **profile** says which API provider and model a kind of capture uses.

```yaml
default: notes
review_profile: youtube
profiles:
  notes:     { backend: freellmapi, model: "${FREELLMAPI_MODEL:-auto}" }
  clippings: { backend: openai,     model: "${OPENAI_MODEL_CLIPPINGS}" }
  youtube:   { backend: openai,     model: "${OPENAI_MODEL_YOUTUBE}" }
  fake:      { backend: fake }
```

### What each profile is for

- **`notes`**: short dictated notes. Provider: FreeLLMApi. Uses `FREELLMAPI_URL`, `FREELLMAPI_MODEL` and `FREELLMAPI_API_KEY`.
- **`clippings`**: AI chats (Gemini and Claude clips that are not YouTube) **and web clips** (articles and other pages). Provider: OpenAI. Uses `OPENAI_API_KEY` and `OPENAI_MODEL_CLIPPINGS`.
- **`youtube`**: YouTube clips, Gemini video chats and the **reviewer**. Provider: OpenAI. Uses `OPENAI_API_KEY` and `OPENAI_MODEL_YOUTUBE`.
- **`fake`**: returns canned JSON and calls nothing. Use it for free dry runs and tests.

### How it works

- **`backend`** is `freellmapi`, `openai` or `fake`. The two real backends speak the same OpenAI-style protocol. They differ only in base URL, key and model.
- **`model`** may use `${NAME}` or `${NAME:-default}`. It is filled from the environment, so from `.env`.
- **To change the provider** of one kind of capture, edit its line. Chats and videos can use different models because they are separate profiles.
- **The document class picks the default profile:** `note` uses `notes`, `ai-chat` and `web-clip` use `clippings`, and the YouTube classes use `youtube`. The flag `--profile` overrides it for a whole run.
- **Unknown fields are rejected.** An old file with `when:` or `backend: claude-code` fails loudly.
- **There is no fallback between providers.** If a provider is down or its budget is used up, its documents stall in `output/`. Change the profile's provider, or raise the budget, to continue.

The lines `retry_delay`, `budget_retry_delay` and `stuck_after_days` in the file are **not used in Stage A**. They matter from Stage B, when Postgres and the queue arrive.

## Options for one run (command line)

- **`--ideas PATH`** (`scan`, `run pipeline`): use this `idea-bucket` checkout instead of `IDEAS_REPO`. Handy for a run on copies.
- **`--docs PATH`** (`run pipeline`, `render`): use this `epiaku-docs` checkout instead of `DOCS_REPO`.
- **`--profile NAME`** (`run pipeline`, `render`, `reason`): use this profile for every note. `--profile fake` costs nothing.
- **`--no-review`** (`run pipeline`, `render`): skip the YouTube reviewer. This saves one LLM call per video.
- **`--limit N`** (`run pipeline`): process at most N notes. Use it to cap spend.
- **`--file NAME`** (`run pipeline`, `scan`): process only the named document from `inbox/`, for example `--file "New chat"`. Repeat it for more. A name that matches nothing gives a warning. See [How to run](../idea-catcher-how-to-run/).
- **`--dry-run`** (`run pipeline`): change no files and commit nothing. **With a real profile it still calls the LLM.**
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
uv run catcher run pipeline --profile fake --no-review --dry-run
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

- **Stage B, Postgres and queue:** the database URL and password go in `.env` (secret). The retry settings go in `profiles.yaml`, where they already are.
- **Stage C, API:** API keys for the callers (`Authorization: Bearer …`), one per device, as `name:scopes:key` in `.env` (secret).
- **Proxmox:** the GitHub fine-grained token, `OPENAI_API_KEY` and the schedule go in `.env` on the LXC, not in Git.
- **Future:** a monthly budget guard per provider, with a warning at 80%, in `profiles.yaml`.
