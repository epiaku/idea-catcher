---
title: "Idea Catcher: How to Run Stage B"
linkTitle: "Idea Catcher: Run Stage B"
description: "How to run the Stage B worker and job queue: start Postgres, add jobs, run the worker on the test repos and on your real repos, publish, stop it, and read the exit codes."
weight: 41
type: docs
---

This page shows how to run what Stage B built: a **Postgres job queue** and a **worker** that does the same work as `run pipeline`, as jobs. The Stage A commands are on the [How to Run page](../idea-catcher-how-to-run/). The settings are on the [configuration page](../idea-catcher-configuration/), the design in the [service architecture](../idea-catcher-service-architecture/).

## In short

1. Start a Postgres and create the tables (`docker compose up -d db`, `catcher db upgrade`).
2. Put a job on the queue: `catcher jobs add pipeline.run`.
3. Run the worker: `catcher worker --once` does every job that is due, then exits.
4. Look at the jobs: `catcher jobs list`.
5. Commit and push the result: `catcher jobs add pipeline.publish`, then the worker again.

Nothing is scheduled yet (that is B6): you add the jobs by hand. Try it first on the test repos (see [Worker, jobs and exit codes](#worker)); that costs nothing.

## Run it on test repos

```bash
uv run catcher testdata reset                         # delete and fresh copy of test data
uv run catcher testdata reset --fresh-llm-and-youtube # also delete all saved llm and youtube api data

export IDEAS_REPO=tmp/ic/idea-bucket
export DOCS_REPO=tmp/ic/epiaku-docs
```

## Run it on your real repos

The same steps, on `IDEAS_REPO` and `DOCS_REPO` from your `.env`. **This calls the real models and, for YouTube clips, YouTube**, so it costs money within the budget caps and the [YouTube gap](../idea-catcher-how-to-run/#youtube-gap) applies. Start small.

```bash
# 0. Postgres and the tables (the data stays in a Docker volume when you stop it)
docker compose up -d db
uv run catcher db upgrade

# 1. see what is in the inbox first, free (Stage A, no database, no model)
uv run catcher run pipeline --profile fake --dry-run

# 2. a small first run: stage at most 3 documents, then run what is due
uv run catcher jobs add pipeline.run --param limit=3    # adds a job to the queue, procdess max 3 docs (queues only, no runs)
uv run catcher jobs add pipeline.run                    # adds a job to the queue to process all docs (queues only, no runs)
uv run catcher worker --once                            # the worker runs the queued job and the jobs it creates
uv run catcher jobs list                                # shows the jobs and their status

# 3. look at the result: output/ in idea-bucket, the pages in epiaku-docs (nothing is committed yet)
git -C /path/to/idea-bucket status --short       # your IDEAS_REPO
git -C /path/to/epiaku-docs status --short       # your DOCS_REPO

# 4. commit; push comes later, so look at the commits first
uv run catcher jobs add pipeline.publish --param push=false
uv run catcher worker --once
git -C /path/to/epiaku-docs log --stat -1

# 5. when you are happy: publish again (nothing new to commit, the commits are pushed)
uv run catcher jobs add pipeline.publish
uv run catcher worker --once

docker compose down                          # when you are done (the data stays)
```

- **Keys and budgets.** `notes` use FreeLLMApi, `clippings` and `youtube` use OpenAI, and the keys have budget caps. A document that cannot be answered is `deferred` (not lost): its job still `succeeded`, and the working copy in `output/` says `stage: deferred` and why. Retry it with `jobs add pipeline.run --param retry_deferred=true`.
- **YouTube clips.** A clip without saved facts gets a `youtube.fetch` job first. The gate lets one fetch through per gap (about 2 minutes), so the other fetch jobs wait in the queue (`jobs list` says `waiting for youtube until <time>`; see [The YouTube gate](#youtube-gate)). `worker --once` leaves a job that waits for a later time. **To work through a batch of clips, run the worker without `--once`** and let it run (stop it with Ctrl-C), or run `worker --once` again later.
- **One more run later:** add `pipeline.run` again (with a `limit` or without). Documents that are already done are not touched.

## Database {#database}

The queue and the state tables live in Postgres. Stage A needs none of this: `run pipeline` and the other commands above never touch the database. The schema commands, the tests, the [worker and the `jobs` commands](#worker) use it.

**Start a Postgres 17 for development** with the `compose.yaml` in the repo root (the image has pgvector, as in the design):

```bash
docker compose up -d db        # start it; the data lives in the volume catcher-pgdata
docker compose ps              # is it running (and healthy)?
docker compose down            # stop it; the data stays, so the next `up` has your tables and jobs
docker compose down -v         # stop it and delete the data
```

If an older container from `docker run ... --name catcher-db` is still running, stop it first (`docker stop catcher-db`): both want port 5432.

This matches the default `DATABASE_URL`, `postgresql+psycopg://catcher:catcher@localhost:5432/catcher`. To use another port or server, set `DATABASE_URL` in `.env` or in the shell (see [Configuration](../idea-catcher-configuration/)). The compose file has only the database for now: the worker joins it in step B7.

**Create and drop the tables:**

```bash
uv run catcher db upgrade        # migrate to the latest revision (head); a REVISION can be given instead
uv run catcher db downgrade -1   # roll back one migration; REVISION is required (a revision id, or -1)
```

`catcher db downgrade base` drops **every table with all its rows**, so it asks for confirmation first (`--yes` skips the question). Without a REVISION the command fails and changes nothing.

After `upgrade` the tables are `jobs`, `job_items`, `job_events`, `resources`, `schedules` and Alembic's `alembic_version`, and `resources` holds the row `youtube`, open (the [YouTube gate](#youtube-gate)); after `downgrade base` only `alembic_version` is left.

**Run the database tests:**

```bash
uv run pytest tests/integration/db -q
```

They start their **own throwaway Postgres container** through testcontainers (`pgvector/pgvector:pg17`) and remove it afterwards, so the development container is not needed and is not touched. They **skip** when Docker is not running. On macOS with Docker Desktop the socket is found automatically if `~/.docker/run/docker.sock` exists (unless `DOCKER_HOST` is set). **The pre-commit hook does not run them**: it runs only `tests/unit` and `tests/component`, so run the database tests yourself before you push anything that touches `modules/queue`, `core/db.py` or `migrations/`.

**Run every check at once:** `scripts/check` (from any folder) runs `ruff check`, `ruff format --check`, `pyright` and then all the tests, and stops at the first failure. The tests run with outgoing network blocked (`tests/support/blocknet.py`): any connection to a host other than localhost, or a DNS lookup of one, raises `NETWORK BLOCKED`, is counted, and makes the run fail even when a test swallowed the error. `scripts/check --fast` runs only `tests/unit` and `tests/component` (no Docker). The database tests skip by themselves when Docker is not running, so `scripts/check` can pass without them: start Docker for the full check. The guard is not loaded by default, so `pytest -m live` by hand still works. GitHub Actions (`.github/workflows/ci.yml`) runs `scripts/check` only when you start it by hand (Actions tab, "Run workflow"); it does not run on push or pull request.

## Worker, jobs and exit codes {#worker}

In Stage B the same work runs as **jobs** in the Postgres queue. `catcher jobs add` puts a job on the queue, `catcher worker` runs the jobs. Nothing is scheduled yet (that is B6): you add the jobs by hand. The worker uses `IDEAS_REPO` and `DOCS_REPO` (or `--ideas`/`--docs`) like `run pipeline`, and the database in `DATABASE_URL` (`postgresql+psycopg://catcher:...@localhost:5432/catcher`).

**A free try on the test repos.** The test repos hold the saved replies and facts of a real run, so this calls no model and no YouTube. The empty key and the unreachable URL make sure a reply that is not saved fails (the document is `deferred`) instead of costing money.

```bash
# 1. a Postgres and the tables (see Database above)
docker compose up -d db
uv run catcher db upgrade

# 2. fresh test repos, one pipeline.run job, and a worker that runs everything that is due, then exits
uv run catcher testdata reset
uv run catcher jobs add pipeline.run           # prints: job queued, version <version>, job id <guid>
OPENAI_API_KEY="" FREELLMAPI_URL=http://127.0.0.1:1/v1 YOUTUBE_OFFLINE=1 \
  uv run catcher worker --once --ideas tmp/ic/idea-bucket --docs tmp/ic/epiaku-docs
uv run catcher jobs list                       # newest first: id, type, status, priority, run_after, reason or error

# 3. commit the result (the test repos have no remote, so no pull and no push)
uv run catcher jobs add pipeline.publish --param push=false --param pull=false
uv run catcher worker --once --ideas tmp/ic/idea-bucket --docs tmp/ic/epiaku-docs
git -C tmp/ic/epiaku-docs log --stat -1        # the pages

docker compose down                            # when you are done (the data stays, `down -v` deletes it)
```

What you see (checked on 2026-10-03): the `pipeline.run` job stages the 43 documents and queues one `llm.reason` job per document (a clip without saved facts gets a `youtube.fetch` job first). **One `worker --once` runs them all**, because it keeps going while jobs are due: it ends with `ran 44 job(s): succeeded=44` after about 2 seconds, all from saved replies. The pages and the moved files are in the repos, but **nothing is committed until `pipeline.publish`**, which makes one commit per repo (`idea-catcher: process the inbox (pipeline.publish)` and `idea-catcher: publish pages (pipeline.publish)`).

**The jobs and their parameters.** Give parameters with `--param KEY=VALUE` (repeat it): `true`/`false` become booleans, whole numbers become integers, the rest stays text. `only` and `requeue` are always lists of names: repeat them for more (`--param only=a --param only=b`); a name is never split on commas, because a file name can hold one. `--priority N` puts a job before others (higher first). `jobs add` checks the job first with the handler's own check: an unknown job type or a parameter the handler would refuse exits with code 2 and queues nothing (`cannot queue pipeline.run: limit must be a whole number of 0 or more, not -1`).

| Job                           | Parameters                                                             | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ----------------------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pipeline.run`                | `limit`, `profile`, `refresh_llm`, `retry_deferred`, `only`, `requeue` | Stages the `inbox/` documents (database row first, then the move to `archive/` and `output/`), copies the artifacts, and queues the next job of each document. The parameters mean what the `run pipeline` options of the same name mean: `--param only=x` is `--file x`, `--param requeue=x` is `--requeue x` (repeat both for more names; an absolute path or a `..` part is refused). `requeue` leaves a document alone while a queued or running job carries it; a document left active with no job (a stuck leftover) is requeued                            |
| `pipeline.publish`            | `pull`, `push` (both `true` by default)                                | Per repo, `idea-bucket` first: commits the managed folders (in `idea-bucket` all of `inbox/`, so also captures that arrived after the last `pipeline.run`, plus `archive/`, `output/`, `failed/`, `duplicates/`, `facts/` and `llm/`; in `epiaku-docs` the page folders and `idea-bucket/artifacts/`), then `git pull --rebase`, then push. A failed rebase is aborted and fails the job; the commit stays local and the next publish pushes it. With `push=true` on a repo without a remote the job fails before any git change (`no git remote to push to ...`) |
| `llm.reason`, `youtube.fetch` | `calculated_name`, `profile`, `refresh_llm` (queued by the worker)     | One document each, by its calculated name (`notes/<name>.md`, `clippings/2026/<name>.md`, or `<name>.md` for a capture directly in `inbox/`). You do not add these by hand                                                                                                                                                                                                                                                                                                                                                                                        |

- **A fresh answer:** `uv run catcher jobs add pipeline.run --param refresh_llm=true` calls the model even when a reply is saved, so it costs money with real keys. With the empty key above it only shows that the documents defer (`OPENAI_API_KEY is not set`). With `refresh_llm=true` a rerun after a crash also skips the reply the first try saved, so **the model can be paid twice**; without it the model is paid once.
- **Dry runs never go through the queue.** `jobs add` refuses a `dry_run` parameter (exit code 2): use `run pipeline --dry-run`.
- **A job's status is not a document's status.** A document that defers (the LLM is down, a budget is used up) or fails for good is an item outcome: its job still `succeeded`. The item states are in the `job_items` table (there is no command for them yet) and in the working copy in `output/` (`stage: deferred`), as in Stage A. A job `failed` means the job itself went wrong: a git error, a parameter the job carries that is wrong, or an unexpected error in the code. When a failed job carries a document (`llm.reason`, `youtube.fetch`), the worker marks that document `failed` with the job's error and moves its working copy to `failed/`, unless another queued or running job carries it; a `requeue` runs it again.
- **A backend that is down or out of budget is not called again for a while.** After one document finds its LLM backend out of its usage limit or budget, or down, the next documents that need that backend are `deferred` without a call until the cool-down ends (`LLM_BLOCK_S`, 10 minutes; a saved reply is still used). Retry them later with `jobs add pipeline.run --param retry_deferred=true`. The worker keeps this in memory only: a restart forgets it.
- **`worker --once` exits 0 even when a job failed.** Look at `jobs list`. A job deferred to a later time (a YouTube gap) is not due, so `--once` leaves it.
- **Exit codes of `catcher worker`:** `0` it stopped normally (also when jobs failed); `1` it lost its one-worker lock (see below), or with `--once` a claim hit a database error (`could not claim a job`) or a job could not be finished (`error=1` in the summary; the reaper puts it back after its lease); `2` another worker runs, `DATABASE_URL` is malformed, or the database cannot be reached.

**Running the worker.** Without `--once` it runs until you stop it, waits `--poll-s` seconds (2) when no job is due, and at the start and every 60 seconds (between jobs) puts back jobs whose lease ran out (the reaper; `--once` reaps once at the start).

- **Stop it with Ctrl-C or `kill` (SIGTERM).** It finishes the job it is running, then exits (exit code 0). A **second** Ctrl-C stops at once: that job stays `running` until its lease ends (`--lease-s`, 120 seconds; a heartbeat renews it while the job runs), then the reaper puts it back on the queue and counts one attempt. After 3 such attempts the job fails, and its document is marked `failed` and moved to `failed/`.
- **One worker at a time.** A second worker on the same database exits at once with code 2: `another worker is already running; run one worker at a time`. The lock lives on one database connection. Before every claim and every reap the worker checks that this connection still holds it; after a Postgres restart or a dropped connection it stops with exit code 1 (`the worker lost its database lock ...`), so a supervisor can start it again and it takes the lock again.
- **No Postgres:** `cannot reach the database in DATABASE_URL` and exit code 2, for the worker and the `jobs` commands. A malformed `DATABASE_URL` gives `DATABASE_URL is not a valid database URL` and exit code 2; no message shows the URL (it holds the password).
- **Do not run `run pipeline` (Stage A) on the checkout a worker uses.** Both move files in `inbox/` and commit; nothing stops that yet (an open item).

## The YouTube gate {#youtube-gate}

The worker keeps the YouTube gap and the breaker (the rules are in [YouTube and the gap between calls](../idea-catcher-how-to-run/#youtube-gap)) in Postgres, in the `resources` row `youtube`, so every worker on the database sees the same gate. The Stage A commands keep their own gate in `youtube-gate.json` in `CATCHER_STATE_DIR`: the two do not see each other (no shared gap, no shared block), so let one of them do the YouTube clips.

```bash
uv run catcher youtube gate                # youtube: open
                                           # youtube: next call allowed at 20:41
                                           # youtube: blocked until 2026-10-05 02:38 (block 1)
uv run catcher youtube gate --import-file  # copy a block of the Stage A gate file to the worker, once
```

What you see (checked on 2026-10-04, on a throwaway database with a fake block):

- **A fetch job that waits for the gate stays queued.** The worker does not take it until the gap or the block is over, so it does not count an attempt and fills no log. `jobs list` shows why: `youtube.fetch  queued  0  2026-10-04T18:38:52+00:00  waiting for youtube until 2026-10-05 02:38`. With only such jobs, `worker --once` ends at once with `ran 0 job(s)`; other job types still run. A `youtube.fetch` you add by hand with `jobs add` does not carry the gate, so it does not wait like this (it is taken and deferred; you do not need to add one).
- **`--import-file` merges** `youtube-gate.json` from `CATCHER_STATE_DIR` into the row. It only makes the gate more careful: of each value (the gap, the end of a block, the block count) it keeps the later or the larger one. It prints `imported: blocked until 2026-10-05 08:38 (block 2)`. Run it again and nothing changes: `not imported: the database already holds this state or a stricter one (youtube: ...; the file: ...)` with exit code 2. With no file: `no state file at <path>: nothing to import` (exit 2). A damaged file is kept as `youtube-gate.corrupt` and imported as closed, as the Stage A gate does.
- **A missing or damaged row means closed.** The gate rewrites the row as a block of `YOUTUBE_BLOCK_HOURS` and logs an ERROR. A time more than 24 hours ahead is damage, not a block: the gate cuts it to 24 hours (and until then the queue does not let it hold the fetch jobs back).
- **The database cannot be reached during a fetch decision:** no fetch is made, the job waits 60 seconds and tries again, and the document stays `waiting_youtube`.
- **A 429 that cannot be written to the row** (the database fails right then): the worker makes no YouTube call for the first breaker step (`YOUTUBE_BLOCK_HOURS`, 6 hours). It keeps that block in memory only, so a restart of the worker forgets it; look at `catcher youtube gate` and the log (`YouTube is blocking us`) before you start the worker again.
- **Exit codes of `catcher youtube gate`:** `0` done; `2` nothing imported (no file, the database already holds this state or a stricter one, a file that cannot be read or repaired), `DATABASE_URL` is malformed, or the database cannot be reached (`cannot reach the database in DATABASE_URL`).
