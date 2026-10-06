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

# 1. see what is in the inbox first, free (no model; it takes the worker's lock, so no worker may run)
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

The queue, the state tables, the YouTube gate and the one-at-a-time lock live in Postgres, and nothing runs without it: the [worker and the `jobs` commands](#worker), `run pipeline` (also `--dry-run`), `render`, `youtube facts` and `youtube gate` stop with exit code 2 when the database cannot be reached. Only `scan` and `reason` work without it (`reason` refuses YouTube clips: they need facts, and the facts need the gate). **The lock and the gate are per database.** Two shells with different `DATABASE_URL`s (the development database and a throwaway one) do not see each other's lock or gate: they are two writers on one checkout and two YouTube gates. Use one database for a set of checkouts.

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

**Run every check at once:** `scripts/check` (from any folder) runs `ruff check`, `ruff format --check`, `pyright` and then all the tests, and stops at the first failure. Every pytest run of this project, `scripts/check` or a plain `uv run pytest`, has outgoing network blocked (`tests/support/blocknet.py`, loaded by `tests/conftest.py`): any connection to a host other than localhost, or a DNS lookup of one, raises `NETWORK BLOCKED`, is counted, and makes the run fail even when a test swallowed the error. `scripts/check --fast` runs only `tests/unit` and `tests/component` (no Docker). The database tests skip by themselves when Docker is not running, so `scripts/check` can pass without them: start Docker for the full check. The guard is on by default; the one way off is the manual live tests: `CATCHER_ALLOW_NETWORK=1 uv run pytest -m live`. Such a run says `BLOCKNET: guard OFF (CATCHER_ALLOW_NETWORK=1)` at the start and at the end, so a shell that kept the variable is noticed: `unset CATCHER_ALLOW_NETWORK` after a live run. What the guard does not see: **Postgres connections** (psycopg uses libpq, C code, not Python sockets; localhost is allowed anyway). That is by design: every test gets a `DATABASE_URL` on a closed local port (`tests/conftest.py`) unless it names its own throwaway test database, so no test reaches your `catcher-db`. And **subprocesses**: the two tests that start a real `catcher worker` process (`tests/integration/db/test_worker_cli.py`) run outside the guard; that process gets the test database, throwaway repo paths and empty API keys, and does not read `.env`. GitHub Actions (`.github/workflows/ci.yml`) runs `scripts/check` only when you start it by hand (Actions tab, "Run workflow"); it does not run on push or pull request.

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
- **A job's status is not a document's status.** A document that defers (the LLM is down, a budget is used up) or fails for good is an item outcome: its job still `succeeded`. The item states are in the `job_items` table (`catcher items list` shows them) and in the working copy in `output/` (`stage: deferred`); see [Item states, stuck and reconcile](#item-states). A job `failed` means the job itself went wrong: a git error, a parameter the job carries that is wrong, or an unexpected error in the code. When a failed job carries a document (`llm.reason`, `youtube.fetch`), the worker marks that document `failed` with the job's error and moves its working copy to `failed/`, unless another queued or running job carries it; a `requeue` runs it again.
- **A backend that is down or out of budget is not called again for a while.** After one document finds its LLM backend out of its usage limit or budget, or down, the next documents that need that backend are `deferred` without a call until the cool-down ends (`LLM_BLOCK_S`, 10 minutes; a saved reply is still used). A used-up budget blocks it for `LLM_BUDGET_BLOCK_S` (6 hours), and a model the backend does not know blocks only that profile. Retry them later with `jobs add pipeline.run --param retry_deferred=true`. The blocks are kept in Postgres (`resources`), so a restart keeps them.
- **`worker --once` exits 0 even when a job failed.** Look at `jobs list`. A job deferred to a later time (a YouTube gap) is not due, so `--once` leaves it.
- **Exit codes of `catcher worker`:** `0` it stopped normally (also when jobs failed); `1` it lost its one-worker lock (see below), or with `--once` a claim hit a database error (`could not claim a job`) or a job could not be finished (`error=1` in the summary; the reaper puts it back after its lease); `2` another worker runs, `DATABASE_URL` is malformed, or the database cannot be reached.

**Running the worker.** Without `--once` it runs until you stop it, waits `--poll-s` seconds (2) when no job is due, and at the start and every 60 seconds (between jobs) puts back jobs whose lease ran out (the reaper; `--once` reaps once at the start).

- **Stop it with Ctrl-C or `kill` (SIGTERM).** It finishes the job it is running, then exits (exit code 0). A **second** Ctrl-C stops at once: that job stays `running` until its lease ends (`--lease-s`, 120 seconds; a heartbeat renews it while the job runs), then the reaper puts it back on the queue and counts one attempt. After 3 such attempts the job fails, and its document is marked `failed` and moved to `failed/`.
- **One worker at a time.** A second worker on the same database, or a worker started while a `run pipeline` runs, exits at once with code 2: `another worker or run is already running; one at a time`. The lock lives on one database connection. Before every claim and every reap the worker checks that this connection still holds it; after a Postgres restart or a dropped connection it stops with exit code 1 (`the worker lost its database lock ...`), so a supervisor can start it again and it takes the lock again.
- **No Postgres:** `cannot reach the database in DATABASE_URL` and exit code 2, for the worker and the `jobs` commands. A malformed `DATABASE_URL` gives `DATABASE_URL is not a valid database URL` and exit code 2; no message shows the URL (it holds the password).
- **`run pipeline` and the worker take the same lock.** `run pipeline` (also `--dry-run`) takes the worker's lock in Postgres before it touches a file, so while a worker runs it exits at once with code 2: `another worker or run is already running; one at a time: nothing was done`; with the database down: `cannot reach the database in DATABASE_URL: nothing was done` (code 2). A worker started during a run exits with code 2 (`another worker or run is already running; one at a time`). `render` takes the same lock (it writes a page into `epiaku-docs`, which a worker's `pipeline.publish` would commit and push), with the same exit codes and messages. A run that loses the lock halfway (a Postgres restart) stops before the next document, commits nothing, says what it had done (`N document(s) were finished and are NOT committed`), and exits with code 1. **The next `run pipeline` does not commit those documents**: it commits only the files it changes itself. They stay uncommitted until the next `pipeline.publish` job commits them (`catcher jobs add pipeline.publish`, then the worker), or until you commit them by hand. The lock is per database: see [The database](#database).

## Item states, stuck and reconcile {#item-states}

On the worker path **the database is the truth** for every document: its row in `job_items` has one of nine states. The worker changes them in one place, and each change writes a `job_events` row.

| State             | What it means                                                                                         |
| ----------------- | ----------------------------------------------------------------------------------------------------- |
| `staging`         | `pipeline.run` made the row and is moving the capture out of `inbox/`                                 |
| `waiting_youtube` | a YouTube clip waits for its `youtube.fetch` job (the facts)                                          |
| `waiting_llm`     | waits for its `llm.reason` job                                                                        |
| `ready`           | allowed in the table, but the worker does not use it yet (`waiting_llm` goes straight to `published`) |
| `published`       | done: the page is in `epiaku-docs` and the working copy in `output/` is that page                     |
| `deferred`        | stalled for a while (LLM down, a budget or usage limit, no facts); retried by `retry_deferred`        |
| `stuck`           | `deferred` for more than `STUCK_AFTER_DAYS` (3) days; still retried                                   |
| `failed`          | failed for good: the file is in `failed/` with an `.error.txt`                                        |
| `duplicate`       | an earlier snapshot of a longer clip, in `duplicates/`                                                |

**The frontmatter shows the state.** After every status change the worker writes three lines into the working copy in `output/` (or the file in `failed/`): `stage`, `stage_reason` and `stage_since`. The Stage A names stay next to them (`analyzed_at`, `deferred_at`, `deferred_reason`). The finished page says `stage: published` and `created_by: idea catcher`, nothing more, because it is the same file as the page in `epiaku-docs`. If the mirror cannot be written, the row still has the state and the next change writes it.

```yaml
stage: deferred                                # a working copy (checked 2026-10-06)
stage_since: '2026-10-06T08:10:58+00:00'
stage_reason: OPENAI_API_KEY is not set
```

**See the items without SQL:**

```bash
uv run catcher items list                    # newest first: name, class, state, since (local time), reason
uv run catcher items list --status stuck     # only one state; --limit N (default 50)
```

**`stuck`.** At every reap (at the start of the worker, also with `--once`, and every 60 seconds) an item that has been `deferred` for `STUCK_AFTER_DAYS` (3) days becomes `stuck`, with a warning event and the reason `deferred for 4 days: <old reason>`. Its `stage_since` is then the time it became stuck. It is not given up: `jobs add pipeline.run --param retry_deferred=true` retries `deferred` **and** `stuck` items, a retry that defers again keeps it `stuck`, and a publish ends it. The retried documents go back through `inbox/`, so a `limit` counts them too: with `limit=0` they wait in `inbox/` (their row keeps its state) until the next `pipeline.run`.

**How the retries add up.**

- **Inside one `llm.reason` job** the LLM service retries a temporary error (a 5xx, a timeout) up to `LLM_MAX_ATTEMPTS` (5) calls. After that the document is `deferred` or `failed`, and the job still `succeeded`.
- **There is no job-level retry layer.** A deferred document waits for a `pipeline.run` with `retry_deferred`. You add that by hand for now; the scheduler adds it in B6.
- **The backend blocks** are the only waiting time: a usage limit or a down backend blocks it for `LLM_BLOCK_S` (600 s), a used-up budget for `LLM_BUDGET_BLOCK_S` (6 hours), and a model the backend does not know blocks only its profile (`openai:clippings`). The documents that need a blocked backend are `deferred` without a call. A block is a row in `resources`, so it survives a worker restart.
- **A crashed job** (the worker died) comes back after its lease and is failed after 3 attempts; that is the only attempt counter.

**`catcher reconcile` rebuilds the rows from the folders**, for a lost or new database. Run it with `--dry-run` first:

```bash
uv run catcher reconcile --dry-run    # says what it would do: created, fixed, missing, skipped; writes nothing
uv run catcher reconcile              # writes the rows and closes the YouTube gate
uv run catcher youtube gate           # youtube: blocked until 16:12 (block 1)
```

- **What it does.** A document in `output/`, `failed/` or `duplicates/` without a row gets one, with the state its folder and its `stage` give (an `info` event `reconcile: created ...`). A row whose file moved gets the state of its new folder (`fixed`). A file it cannot read is `skipped` with the reason.
- **What it does not do.** It never deletes a file or a row and never changes a file. A row whose file is gone is only reported (`missing`). It cannot rebuild attempts, tokens, models or events.
- **The folder wins.** A stale working copy can move a row back to an older state, and a `failed` row whose file stayed in `output/` goes back to an active state with no job. Look at the `--dry-run` list first.
- **No jobs.** A row it creates in an active state (`waiting_llm`, `waiting_youtube`) gets no job. Run `jobs add pipeline.run --param retry_deferred=true`: it picks up every `deferred` and `stuck` row and every `waiting_youtube`, `waiting_llm` or `ready` row that no queued or running job carries. It starts each one again from its copy in `archive/`, or, when there is none, queues its next job from the working copy in `output/` (counted as `adopted`). A row with neither copy is left as it is, and a warning names it. `--param requeue=NAME` does one document, and only from `archive/`.
- **It needs the database and the run lock**, like `run pipeline`: exit code 2 while a worker or a run is running, or when the database cannot be reached; 1 when a database error stopped it.
- **It closes the YouTube gate** for `YOUTUBE_BLOCK_HOURS` (6 hours), because a rebuilt database starts with the gate open and may have lost a block. Every run without `--keep-gate` closes it again from now, so use `--keep-gate` when you run it a second time. `--dry-run` never touches the gate.

What you see (checked on 2026-10-06, on a throwaway database and test repos): after `db downgrade base --yes` and `db upgrade`, `reconcile --dry-run` lists `created` for the four documents in `output/` and ends with `summary: created=4 fixed=0 missing=0 skipped=0 (dry run: nothing was written)`; `reconcile` writes them (as `published`) and prints `YouTube gate closed until 16:12 (reconcile; use --keep-gate to skip)`; a second `reconcile` prints `summary: created=0 fixed=0 missing=0 skipped=0`.

## The YouTube gate {#youtube-gate}

There is one YouTube gate: the gap and the breaker (the rules are in [YouTube and the gap between calls](../idea-catcher-how-to-run/#youtube-gap)) live in Postgres, in the `resources` row `youtube`. The worker, `run pipeline` and `youtube facts` all go through it, so they share the gap and every one of them sees a block. Without the database no call to YouTube is made.

```bash
uv run catcher youtube gate                # youtube: open
                                           # youtube: next call allowed at 20:41
                                           # youtube: blocked until 14:04 (block 1)   (the date only when not today)
```

**The old gate file.** Until 2026-10-04 the Stage A commands kept their own gate in `~/.catcher/state/youtube-gate.json`. Nothing reads it any more, and `--import-file` is gone: delete it with `rm -r ~/.catcher/state` (it may also hold an old `pipeline.lock`). An old `CATCHER_STATE_DIR` line in `.env` is ignored.

What you see (checked on 2026-10-04, on a throwaway database with a fake block):

- **A fetch job that waits for the gate stays queued.** The worker does not take it until the gap or the block is over, so it does not count an attempt and fills no log. `jobs list` shows why: `youtube.fetch  queued  0  2026-10-04T18:38:52+00:00  waiting for youtube until 2026-10-05 02:38`. With only such jobs, `worker --once` ends at once with `ran 0 job(s)`; other job types still run. A `youtube.fetch` you add by hand with `jobs add` waits in the same way (you do not need to add one: the pipeline queues them).
- **A missing or damaged row means closed** while the database lives: the gate rewrites the row as a block of `YOUTUBE_BLOCK_HOURS` and logs an ERROR. A time more than 24 hours ahead is damage, not a block: the gate cuts the end of a gap or a block to 24 hours and a block time (`blocked_at`) to now (and until then the queue does not let it hold the fetch jobs back).
- **A new or rebuilt database starts open.** `db upgrade` seeds the row open, so a fresh install does not wait 6 hours for nothing. If you rebuild the database (or run `db downgrade` past `0004` and upgrade again) while YouTube blocks this IP, the block in the row is gone: check the log for `YouTube is blocking us` and wait before you queue clips or run `run pipeline` on them. From B5 on, `catcher reconcile` closes the gate after a rebuild.
- **The database cannot be reached during a fetch decision:** no fetch is made, the job waits 60 seconds and tries again, and the document stays `waiting_youtube`.
- **A 429 that cannot be written to the row** (the database fails right then): the worker makes no YouTube call for the first breaker step (`YOUTUBE_BLOCK_HOURS`, 6 hours) and keeps that block in memory. As soon as the gate answers again, the worker writes the block into the row (before anything else), and from then on `catcher youtube gate` shows it and a restart keeps it. Until then only the log (`YouTube is blocking us, and the gate could not record the block`) shows it, and a restart forgets it: read the log before you restart a worker after a database failure.
- **Exit codes of `catcher youtube gate`:** `0` done; `2` `DATABASE_URL` is malformed (`DATABASE_URL is not a valid database URL`), or the database cannot be reached (`cannot reach the database in DATABASE_URL`).
