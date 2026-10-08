---
title: "Idea Catcher: How to Run Stage B"
linkTitle: "Idea Catcher: Run Stage B"
description: "How to run the Stage B worker and job queue: start Postgres, add jobs, run the worker on the test repos and on your real repos, schedule it, publish, backfill old YouTube links, stop it, and read the exit codes."
weight: 41
type: docs
---

This page shows how to run what Stage B built: a **Postgres job queue** and a **worker** that does the work of the pipeline as jobs. Since B5b (2026-10-06) `run pipeline` is that same worker path in one process: it queues `pipeline.run`, runs the worker until nothing is due, then queues and runs `pipeline.publish` (see [`run pipeline`](../idea-catcher-how-to-run/#run-pipeline-the-whole-flow)). The Stage A commands are on the [How to Run page](../idea-catcher-how-to-run/). Since Stage C (2026-10-08) the API starts and reads the same jobs over HTTP: [How to Run the API](../idea-catcher-how-to-run-api/). The settings are on the [configuration page](../idea-catcher-configuration/), the design in the [service architecture](../idea-catcher-service-architecture/).

## In short

1. Start a Postgres and create the tables (`docker compose up -d db`, `catcher db upgrade`).
2. Put a job on the queue: `catcher jobs add pipeline.run`.
3. Run the worker: `catcher worker --once` does every job that is due, then exits.
4. Look at the jobs: `catcher jobs list`.
5. Commit and push the result: `catcher jobs add pipeline.publish`, then the worker again.

Since B6 (2026-10-07) the worker can also queue these jobs itself on a cron schedule (see [Scheduling](#scheduling)), since B7 (2026-10-07) it runs in Docker Compose (see [Run it in Docker](#docker)), and since B8 (2026-10-08) `catcher youtube import` releases the old YouTube links of the docs a few a day (see [Backfill YouTube links](#backfill)); without the `SCHEDULE_*` variables nothing is scheduled and you add the jobs by hand. Try it first on the test repos (see [Worker, jobs and exit codes](#worker)); that costs nothing.

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

This matches the default `DATABASE_URL`, `postgresql+psycopg://catcher:catcher@localhost:5432/catcher`. To use another port or server, set `DATABASE_URL` in `.env` or in the shell (see [Configuration](../idea-catcher-configuration/)). Since B7 the compose file also has `migrate` and `worker`: `docker compose up -d db` starts only the database (see [Run it in Docker](#docker) for the whole stack).

**Create and drop the tables:**

```bash
uv run catcher db upgrade        # migrate to the latest revision (head); a REVISION can be given instead
uv run catcher db downgrade -1   # roll back one migration; REVISION is required (a revision id, or -1)
```

`catcher db downgrade base` drops **every table with all its rows**, so it asks for confirmation first (`--yes` skips the question). Without a REVISION the command fails and changes nothing.

After `upgrade` (head revision `0006`) the tables are `jobs`, `job_items`, `job_events`, `resources`, `schedules`, `backfill_videos` (since B8: the [backfill backlog](#backfill)) and Alembic's `alembic_version`, and `resources` holds the row `youtube`, open (the [YouTube gate](#youtube-gate)); after `downgrade base` only `alembic_version` is left.

**Run the database tests:**

```bash
uv run pytest tests/integration/db -q
```

They start their **own throwaway Postgres container** through testcontainers (`pgvector/pgvector:pg17`) and remove it afterwards, so the development container is not needed and is not touched. They **skip** when Docker is not running. On macOS with Docker Desktop the socket is found automatically if `~/.docker/run/docker.sock` exists (unless `DOCKER_HOST` is set). **The pre-commit hook does not run them**: it runs only `tests/unit` and `tests/component`, so run the database tests yourself before you push anything that touches `modules/queue`, `core/db.py` or `migrations/`.

**Run every check at once:** `scripts/check` (from any folder) runs `ruff check`, `ruff format --check`, `pyright` and then all the tests, and stops at the first failure. Every pytest run of this project, `scripts/check` or a plain `uv run pytest`, has outgoing network blocked (`tests/support/blocknet.py`, loaded by `tests/conftest.py`): any connection to a host other than localhost, or a DNS lookup of one, raises `NETWORK BLOCKED`, is counted, and makes the run fail even when a test swallowed the error. `scripts/check --fast` runs only `tests/unit` and `tests/component` (no Docker). The database tests skip by themselves when Docker is not running, so `scripts/check` can pass without them: start Docker for the full check. The guard is on by default; the one way off is the manual live tests: `CATCHER_ALLOW_NETWORK=1 uv run pytest -m live`. Such a run says `BLOCKNET: guard OFF (CATCHER_ALLOW_NETWORK=1)` at the start and at the end, so a shell that kept the variable is noticed: `unset CATCHER_ALLOW_NETWORK` after a live run. What the guard does not see: **Postgres connections** (psycopg uses libpq, C code, not Python sockets; localhost is allowed anyway). That is by design: every test gets a `DATABASE_URL` on a closed local port (`tests/conftest.py`) unless it names its own throwaway test database, so no test reaches your `catcher-db`. And **subprocesses**: the two tests that start a real `catcher worker` process (`tests/integration/db/test_worker_cli.py`) run outside the guard; that process gets the test database, throwaway repo paths and empty API keys, and does not read `.env`. GitHub Actions (`.github/workflows/ci.yml`) runs `scripts/check` only when you start it by hand (Actions tab, "Run workflow"); it does not run on push or pull request.

## Worker, jobs and exit codes {#worker}

In Stage B the same work runs as **jobs** in the Postgres queue. `catcher jobs add` puts a job on the queue, `catcher worker` runs the jobs. The worker (without `--once`) can also queue them on a schedule: see [Scheduling](#scheduling). The worker uses `IDEAS_REPO` and `DOCS_REPO` (or `--ideas`/`--docs`) like `run pipeline`, and the database in `DATABASE_URL` (`postgresql+psycopg://catcher:...@localhost:5432/catcher`).

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
| `ideas.pull`                  | none                                                                   | Pulls `idea-bucket`, so captures pushed from the phone arrive in `inbox/`. It first **commits the worker's managed folders locally** (as `pipeline.publish` does, so the pull never has to stash the worker's own changes), then runs `git pull --rebase`; it **never pushes**. A checkout without a remote is a no-op (`succeeded`, `no git remote`). A checkout that is not on a branch (a rebase or merge you started by hand, a detached HEAD, unmerged files) fails the job and is left untouched. A failed pull (no network, or a remote edit of a file the worker changed) fails the job with git's message; git aborts the rebase, so your files stay as they were and the local commit stays for the next publish. Processes nothing: the next `pipeline.run` does. Queued by `SCHEDULE_IDEAS_PULL`, or by hand |
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
- **`run pipeline` and the worker take the same lock.** `run pipeline` (also `--dry-run`) takes the worker's lock in Postgres before it touches a file, so while a worker runs it exits at once with code 2: `another worker or run is already running; one at a time: nothing was done`; with the database down: `cannot reach the database in DATABASE_URL: nothing was done` (code 2). A worker started during a run exits with code 2 (`another worker or run is already running; one at a time`). `render` takes the same lock (it writes a page into `epiaku-docs`, which a worker's `pipeline.publish` would commit and push), with the same exit codes and messages. A run that loses the lock halfway (a Postgres restart) stops before the next job, commits nothing, says what it had done (`N document(s) were finished and are NOT committed`), and exits with code 1. They stay uncommitted until the next `pipeline.publish` job commits them: the next `run pipeline` (its own publish commits everything under the managed folders), or `catcher jobs add pipeline.publish` and the worker, or you commit them by hand. **Ctrl-C during `run pipeline`** puts the job it was running back in the queue and publishes nothing (exit code 1, `interrupted: N job(s) left queued, run catcher worker --once to finish`): `catcher worker --once` finishes the documents, and the next `run pipeline` commits them. A `run pipeline` refuses to start (exit code 2, nothing done) while a `pipeline.run` or `pipeline.publish` job of an earlier run is still queued; finish it with `catcher worker --once` first. The lock is per database: see [The database](#database).

## Scheduling {#scheduling}

Since B6 (2026-10-07) `catcher worker` (not `--once`) runs a scheduler in a thread next to its job loop. The scheduler **only queues jobs**; the worker runs them, one at a time, like jobs you add by hand. Three variables in `.env` (or the shell) set it up, one cron string each (five fields: minute, hour, day of month, month, day of week, as `croniter` reads them):

```bash
SCHEDULE_IDEAS_PULL="*/30 * * * *"           # queues ideas.pull: pull idea-bucket, the new captures arrive in inbox/
SCHEDULE_PIPELINE_RUN="0 8,12,17,21 * * *"   # queues pipeline.run with retry_deferred=true: process the inbox
SCHEDULE_PUBLISH="0 6,12,18,23 * * *"        # queues pipeline.publish with pull=true, push=true: commit and push
SCHEDULE_TIMEZONE=Europe/Amsterdam           # the times above are wall-clock times here (the default)
SCHEDULE_TICK_S=30                           # how often the scheduler checks for a due slot (the default)
```

- **Empty means off.** All three are empty by default, so a worker without them schedules nothing. Each one can be on or off on its own.
- **What each one queues.** `SCHEDULE_IDEAS_PULL`: an `ideas.pull` job (no parameters). `SCHEDULE_PIPELINE_RUN`: a `pipeline.run` with `retry_deferred=true`, so every scheduled run also retries the `deferred` and `stuck` documents. `SCHEDULE_PUBLISH`: a `pipeline.publish` with `pull=true` and `push=true`. Pulling, processing and publishing have separate schedules, so the pull can run often and the publish a few times a day.
- **The timezone is explicit.** `SCHEDULE_TIMEZONE` (default `Europe/Amsterdam`) is an IANA name; the slots are wall-clock times there, whatever the machine's own timezone is.
- **When a slot fires.** The scheduler checks every `SCHEDULE_TICK_S` seconds (30), so a slot fires up to 30 seconds after its time. In one tick each schedule is checked on its own, so on a slot that two schedules share, one can fire a tick (30 s) later than the other.
- **A new schedule fires nothing until its next slot.** The first time the worker sees a schedule it stores "now" as its last fire (`schedule ideas_pull: new, starts now; the next slot fires`), so starting a worker never fires at once: with `* * * * *` the first job comes at the next whole minute.
- **Changing a schedule can fire it at once.** The last fire is kept per schedule name in `schedules`, so a schedule you turn off and on again, or give a new cron string or timezone, still has its old last fire: the next tick may queue one catch-up job at once instead of waiting for the next slot. Only a schedule never seen before starts "now".
- **A missed slot runs once.** After downtime (the worker stopped, the machine asleep) the first tick queues **one** job per schedule that missed a slot, however many slots it missed, and then goes on with the normal slots. The last fire of each schedule lives in the `schedules` table in Postgres, so a restart keeps it.
- **A slot whose previous job is still queued or running is consumed.** Each schedule queues its jobs with the dedupe key `schedule:<name>`. When the job of the previous slot is still queued or running, no second job is queued (`job ... is still running, no second job`), and the slot counts as handled. Nothing piles up.
- **Daylight saving time.** On the spring-forward day a slot in the missing hour (`30 2 * * *` in Europe/Amsterdam) fires **once**, at the shifted time (03:30). On the fall-back day a slot in the repeated hour fires once, not twice, at its first (summer-time) occurrence; a schedule like `*/30 * * * *` therefore skips the slots of the repeated hour (the second 02:00 and 02:30, winter time) that day. Both are covered by tests (`tests/unit/test_schedule_rules.py`) and a minute-by-minute sweep of the rules, not by a live worker. `catcher schedules` shows a spring-gap slot as `02:30` in `next due`, although it fires at 03:30.
- **A bad cron string or timezone stops the worker before it starts**: exit code 2 and a message that names the variable, for example `SCHEDULE_PUBLISH: '0 8 * * * *' is not a valid cron string (five fields)` or `SCHEDULE_TIMEZONE: unknown timezone 'Mars/Base'`. Six fields (with seconds) are refused.
- **One scheduler.** It runs only in the worker that holds the one-worker lock, so there is never a second one. A database error in a tick is logged once and the scheduler tries again on the next tick; it never stops the worker. When the worker stops (Ctrl-C, SIGTERM, a lost lock) it stops the scheduler thread and waits for it.

- **The order inside one slot.** When `SCHEDULE_PIPELINE_RUN` and `SCHEDULE_PUBLISH` share a slot (12:00 in the example above), the `pipeline.publish` runs right after the `pipeline.run` job, **before** the `llm.reason` jobs that run queues (they are queued later, and the queue goes oldest first). So that publish commits the staged documents, and the pages of that run are committed and pushed by the **next** publish. Give the publish its own times (a little after the run) if you want the pages out at once.
- **A document that keeps deferring is retried by every scheduled run.** Each `pipeline.run` moves it back through `inbox/` and updates its working copy in `output/`, so every publish after it makes a small commit in `idea-bucket`. The LLM itself is not hammered: while a backend is blocked (`LLM_BLOCK_S`, 10 minutes) the retried documents defer without a call, and the log has one `LLM ... is not called again until ...` WARNING per block plus one line per retried document.

**What you see** (checked on 2026-10-07, on a throwaway Postgres, copies of the test repos with local bare remotes, an unreachable LLM, and all three variables set to `* * * * *`):

- The worker started at 09:34:23 logged one line per schedule (`schedule publish: '* * * * *' Europe/Amsterdam, next 09:35`) and `new, starts now; the next slot fires` for each. Nothing fired until the 09:35 slot; the tick at 09:35:23 queued `ideas.pull`, `pipeline.run` and `pipeline.publish` (`schedule ideas_pull: slot 2026-10-07T07:35:00+00:00, queued ideas.pull job ...`), and the worker ran them.
- A capture pushed from another clone into the `idea-bucket` remote at 09:34:42 was pulled by that `ideas.pull` and staged by the `pipeline.run` right after it. A second capture pushed while the worker was stopped arrived with the first pull after the restart.
- The laptop slept from 09:36 to 09:52: on waking, each schedule fired **once** (slot 09:52), not 16 times. The same after `kill` (SIGTERM; the worker stopped within a second, `worker ... stopped`; exit code 0 was seen in a separate foreground run) and 18 minutes down: the restart queued exactly one job per schedule at once, then the normal slots.
- The deferred captures (no LLM) were retried by every scheduled run; the LLM was called once per 10-minute block, the other runs deferred without a call.
- `catcher publish` while the worker ran: exit 2, `another worker or run is already running; one at a time: nothing was done`. With the worker stopped: `committed: ideas yes, docs no` / `pushed: no (without --push)`, then `nothing to commit`, then `catcher publish --push` pushed the local commit (`pushed: yes`).

**See the schedules:**

```bash
uv run catcher schedules
# name          cron       timezone          last fired        next due
# ideas_pull    * * * * *  Europe/Amsterdam  2026-10-07 07:36  due now
# pipeline_run  * * * * *  Europe/Amsterdam  2026-10-07 07:36  due now
# publish       off        -                 -                 -
```

It reads the same variables as the worker (so run it with the same `.env`) and the `schedules` table; it is read-only and works while a worker runs. **`last fired` is in UTC, `next due` in `SCHEDULE_TIMEZONE`** (the example above is illustrative, as at 09:52 in Amsterdam, which is 07:52 UTC; there `publish` is shown as off). `next due` says `due now` when a slot has passed that the scheduler has not handled yet (it will at its next tick, or at the next start of the worker), `never` in `last fired` means the worker has not seen that schedule yet, and `off` means the variable is empty. A bad cron string or timezone exits 2 with the same message as the worker; no database exits 2.

**Publish by hand: `catcher publish`.**

```bash
uv run catcher publish            # commit the managed folders of both repos; no pull, no push
uv run catcher publish --push     # commit, then pull (rebase) and push both repos
```

It is the publish half of `run pipeline`: it takes the worker's lock, queues one `pipeline.publish` (never a `pipeline.run`), runs it and prints per repo what was committed and whether it pushed (`committed: ideas yes, docs no` and `pushed: yes`, or `pushed: no (without --push)`); with nothing to commit it prints `nothing to commit` and exits 0. A push that fails is reported per repo (`push FAILED for: ...`, the commit stays local) and exits 1. It refuses like `run pipeline` (exit 2, nothing done) while a worker or a run holds the lock, when the database cannot be reached, or while an earlier `pipeline.run` or `pipeline.publish` job is still queued. **So stop the worker first**; while a worker runs, `SCHEDULE_PUBLISH` (or `catcher jobs add pipeline.publish`) is the way to publish.

## Run it in Docker {#docker}

Since B7 (2026-10-07) `docker compose up -d --build` starts the whole Stage B stack: `db` (Postgres), `migrate` (runs `catcher db upgrade` once and exits) and `worker` (`catcher worker` with its schedules). The worker clones `idea-bucket` and `epiaku-docs` into a volume on its first start, so the same `compose.yaml` runs on the Mac and later on a server.

**You need:** Docker with Compose v2, and two GitHub repos you own (the real `idea-bucket` and `epiaku-docs`, or two throwaway repos to try it). The worker publishes with `SCHEDULE_PUBLISH`, so use throwaway repos until you trust the schedules.

**First start:**

1. Make a fine-grained personal access token on GitHub: owner `epiaku`, **only** the two repos, permission **Contents: Read and write**, nothing else.
2. Put this in `.env` (copy `.env.example`; the file stays out of git and out of the image):

```bash
IDEAS_REMOTE=https://github.com/epiaku/idea-bucket.git
DOCS_REMOTE=https://github.com/epiaku/epiaku-docs.git
GITHUB_TOKEN=github_pat_...                  # the token: never put it inside a remote URL
SCHEDULE_IDEAS_PULL="*/30 * * * *"           # the schedules (see Scheduling above); empty means off
SCHEDULE_PIPELINE_RUN="0 8,12,17,21 * * *"
SCHEDULE_PUBLISH="0 6,12,18,23 * * *"
FREELLMAPI_URL=http://10.10.60.12:3001/v1    # must be reachable from inside the container (see below)
# DB_PORT=5433                               # see the port note below
```

3. Start it:

```bash
docker compose up -d --build
```

The worker's entrypoint clones both repos into the `repos` volume (`/data/repos/idea-bucket` and `/data/repos/epiaku-docs`) when they are not there yet, then starts the worker. Later starts find the clones and do not clone again.

**Port note: `DB_PORT`.** The compose `db` publishes Postgres on host port `DB_PORT`, default **5432**. If something already listens on 5432 (for example a `docker run` container named `catcher-db`), `docker compose up` fails with a port-already-allocated error. Either stop the other one first, or set `DB_PORT=5433` in `.env`. Host `catcher` commands then need the same port in `DATABASE_URL` (`postgresql+psycopg://catcher:catcher@localhost:5433/catcher`; with `DB_PASSWORD` set, that password). Inside the compose network the containers always use `db:5432`, so `DB_PORT` only matters on the host.

**The db port is published on `127.0.0.1` only** (`DB_BIND`, default `127.0.0.1`), so the host `catcher` commands reach it on `localhost` and nothing else on the network does. On a server, set a real `DB_PASSWORD` in `.env` before the first `up` (Postgres applies it only when the volume is first created; changing it later breaks `migrate` and `worker`). Set `DB_BIND=0.0.0.0` only when another machine on the LAN really needs the database, and then never with the dev password. The `db` service restarts with Docker (`restart: unless-stopped`) like the worker, so after a reboot both come back.

**The LLM must be reachable from the container.** `localhost` inside the container is the container itself. Point `FREELLMAPI_URL` at the LAN address of the FreeLLMApi, or use `http://host.docker.internal:3001/v1` when it runs on the Mac. The OpenAI profiles need `OPENAI_API_KEY` in `.env` as usual. All of `.env` is read by the containers; `DATABASE_URL`, `IDEAS_REPO` and `DOCS_REPO` are set by `compose.yaml` and win over the host values in `.env`.

**What you see:**

```bash
docker compose ps                              # db healthy, migrate Exited (0), worker healthy after about a minute
docker compose logs -f worker                  # the clone lines on the first start, the schedule lines, then the jobs
docker compose exec worker catcher schedules   # cron, timezone, last fired (UTC), next due
docker compose exec worker catcher jobs list
docker compose exec worker catcher items list
docker compose exec worker catcher health      # "worker running", exit 0
```

`worker` shows `healthy` once `catcher health` finds the worker's lock in the database (the smoke run below saw it healthy 6 to 7 seconds after `up`; the check runs every 30 s and has a 60 s start period). When the migration or a clone fails, the worker does not start; `docker compose logs migrate worker` says why.

**The host `catcher` commands still work.** The `db` service publishes Postgres on `localhost:DB_PORT`, so `uv run catcher jobs list`, `items list`, `schedules` and `db upgrade` on the host read the same database as the worker (the host `DATABASE_URL` default matches the compose defaults, see `.env.example`). They read the host's own clones (`IDEAS_REPO` in your host `.env`), not the ones in the volume.

**One stack per pair of remotes.** Run one stack against the real `idea-bucket` and `epiaku-docs`: not a Mac stack and a server stack at the same time (two databases, two locks: both process the same inbox, pay the LLM twice and make conflicting commits), and remember that `restart: unless-stopped` brings a Mac stack back whenever Docker Desktop starts (`docker compose down` it when the server takes over). Do not mix the host's clones and the stack's clones against one database either: the queue and the item states would describe one clone while the files move in the other.

**Running by hand: inside the stack.** One worker or run at a time holds the lock, so `catcher run pipeline` and `catcher publish` refuse while the worker runs (exit 2, `another worker or run is already running`; the smoke run saw this message from `docker compose exec worker catcher run pipeline`). While the stack runs, work on the stack's clones, not the host's: stop the worker, run the command in a one-off worker container (same environment, the same `repos` volume; the entrypoint finds the clones and does not clone again), then start the worker:

```bash
docker compose stop worker
docker compose run --rm worker catcher publish --push     # pull (new captures) and push
docker compose run --rm worker catcher run pipeline       # process the inbox (add --push to push as well)
docker compose run --rm worker catcher publish --push     # commit and push the result
docker compose start worker
```

The smoke run does exactly this (step (i)): with the worker stopped, a capture pushed to the remote was pulled by `publish --push`, `run pipeline` exited 0 and the next `publish --push` pushed the commit; `--rm` leaves no container behind, and the worker was healthy again after `up -d worker`. If a by-hand command says an earlier run's job is still queued, a scheduled job had not run yet when you stopped the worker: `docker compose start worker`, wait for it to finish, then stop it again (or `docker compose run --rm worker catcher worker --once`).

Or do not stop anything: queue the work and let the worker run it, `docker compose exec worker catcher jobs add pipeline.run` (or `pipeline.publish`); from the host `uv run catcher jobs add ...` does the same, as it only writes the queue.

**Stopping and updating:**

| Command | What it does |
| --- | --- |
| `docker compose up -d --build` | Rebuild the image after a code change and restart what changed. The queue and the clones stay |
| `docker compose stop worker` / `start worker` | Stop and start only the worker (the schedule catches up once after a stop, see Scheduling) |
| `docker compose down` | Remove the containers and the network. **The data stays** |
| `docker compose down -v` | **Deletes the volumes: the queue and the cloned repos.** Anything the worker committed but did not push is lost with them (publish with `push=true` first, or look at `git -C /data/repos/idea-bucket status` inside the worker) |

`docker compose stop` gives the worker 120 seconds (`stop_grace_period`) to finish its current job before Docker kills it; the worker stops on SIGTERM like on Ctrl-C.

**Where the data lives.** Two named volumes, prefixed by the compose project name (the folder name, `idea-catcher`, unless you set `-p` or `COMPOSE_PROJECT_NAME`): `<project>_catcher-pgdata` (Postgres: the queue, items, schedules, the YouTube gate) and `<project>_repos` (the two clones, under `/data/repos`). `docker volume ls` shows them. The worker runs as the non-root user `catcher` (uid 1000).

**First start fails:**

| You see | Why | What to do |
| --- | --- | --- |
| `entrypoint: cloning IDEAS_REMOTE (...) failed with exit code N` (or `DOCS_REMOTE`), plus git's message | The clone failed: a wrong URL, a token without access to that repo, or no network | Fix the remote or the token in `.env`, then `docker compose up -d`. Nothing half-cloned is left behind |
| `entrypoint: IDEAS_REMOTE (https://***@...) carries credentials; put the token in GITHUB_TOKEN, not in the URL` | The remote has `user:token@` in it; it is refused so the token never lands in `.git/config` | Remove the credentials from the URL and put the token in `GITHUB_TOKEN` |
| `entrypoint: ... is not a git checkout and IDEAS_REMOTE is not set` | The folder is not there yet and there is no remote to clone from | Set `IDEAS_REMOTE` / `DOCS_REMOTE` in `.env` |
| `entrypoint: /data/repos/... has no .git and is not empty; leaving it untouched` | A folder with files but no `.git` is never touched | Delete that folder, or the `repos` volume (`docker compose down -v`), to clone again |
| `Bind for 0.0.0.0:5432 failed: port is already allocated` | Another Postgres uses 5432 | Set `DB_PORT` in `.env` or stop the other one (see the port note) |
| `worker` stays `unhealthy` or restarts | No worker holds the lock: it crashed, or exits on startup | `docker compose logs worker`; a bad `SCHEDULE_*` value exits 2 and names the variable |

**The smoke run: `bash scripts/compose-smoke`.** A real compose run on throwaway copies that proves the worker clones, pulls, runs, publishes and stops cleanly. It uses its own compose project, its own image tag (`idea-catcher:smoke-...`), a random free port and local bare repos as remotes; it never reads your `.env`, calls no LLM, YouTube or GitHub, and does not touch a `catcher-db` on 5432 or your own stack. It prints `PASS` or `FAIL` per step and removes what it made (containers, volumes, network, image, temp dir), also on failure.

- **What it proves** (runs 5 and 9 on 2026-10-07, all steps passed): `migrate` exits 0 and the worker is `healthy`; both repos are cloned; all three schedules fired (`last fired` shows a date) and no job failed; a capture pushed to the remote is pulled by the worker, and the worker's publish commits are pushed back; the dummy token is in no log, image history, image config or `.git/config`; `catcher run pipeline` next to the worker exits 2 with the one-worker message; `docker compose stop worker` exits cleanly within the grace period; and after the worker was down for 285 s (four or more missed slots) a restart queued exactly one catch-up job per schedule, with the clones and the queue intact; and the by-hand run inside the stack (step (i), see Running by hand) works: `docker compose run --rm worker catcher publish --push`, `... run pipeline` and `publish --push` exit 0, pull a new capture, push the run's commit and leave no container. `bash scripts/compose-smoke --selftest` checks the assertion logic alone (no Docker).
- **What it costs:** Docker only, no money. A cold build is slow: about 4 to 8 minutes for the whole run, and the image is about 790 MB. The BuildKit build cache stays afterwards (`docker builder prune` clears it).
- **`bash scripts/compose-smoke --selftest`** checks only the script's own assertion logic on sample outputs, without Docker.

**What is not covered:**

- A stop while a long job is running: the smoke stop hits an idle worker (it proves SIGTERM reaches the worker and exit code 0). Finishing the current job inside the 120 s grace period is covered only by the runner's own tests, not by a live container.
- `catcher health` only proves that a worker holds the lock. It does not prove that the scheduler thread keeps ticking. The unhealthy case (the lock lost) was not run live.
- No ssh deploy key: the clones use https and the token only.
- No `deploy.sh` (a later stage). The `api` service came with Stage C (2026-10-08): see [How to Run the API](../idea-catcher-how-to-run-api/).
- A real YouTube fetch from the container: `yt-dlp` and Deno are in the image, but nothing in B7 fetched a video (see the open items in the [service architecture](../idea-catcher-service-architecture/#mvp-stage-b)).

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
| `stuck`           | `deferred` for more than `STUCK_AFTER_DAYS` (3) days; still retried, except a backfill item (below)   |
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
- **There is no job-level retry layer.** A deferred document waits for a `pipeline.run` with `retry_deferred`. Every scheduled `pipeline.run` carries it (see [Scheduling](#scheduling)); without a schedule you add it by hand.
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

## Backfill YouTube links {#backfill}

Since B8 (2026-10-08) `catcher youtube import` catches up on the old YouTube links: the videos that are linked somewhere in `epiaku-docs` but never got a page of their own. It is a **gap finder** (which linked videos have no page yet) plus a **throttled catch-up** (a few of them a day go through the normal pipeline, behind the new clips and through the same YouTube gate). Nothing new talks to YouTube or the LLM: the release only writes clip notes, and the pipeline does the rest as for any clip. The one exception is `--channel` (below), which you run by hand.

```bash
uv run catcher youtube import --dry-run        # 1. what it would find and add; writes nothing
uv run catcher youtube import                  # 2. fill the backlog (the table backfill_videos); writes no files
uv run catcher youtube backlog                 # 3. pending and released counts, the oldest pending videos
uv run catcher youtube import --limit 5        # 4. release at most 5 per rolling 24 hours as clip notes
uv run catcher youtube import --release        #    the same with BACKFILL_DAILY_LIMIT (default 10) as the cap
```

**The options.** `catcher youtube import [--docs PATH] [--ideas PATH] [--dry-run] [--limit N | --release] [--channel URL ... --max-videos N]`. `--docs` and `--ideas` default to `DOCS_REPO` and `IDEAS_REPO` (a missing folder: exit 2). Without `--limit` or `--release` it only scans and stores. `catcher youtube backlog [--limit N]` shows `pending N, released M` and the oldest N pending videos (default 20) with the page they were found in; it is read-only. Neither command takes the worker's lock, so they work while a worker runs.

**What is found.** Every markdown file under the docs repo (`.git` skipped; a symlink that points outside the repo skipped) is read, frontmatter and body, for YouTube video links: `watch?v=` (also with `&list=`), `youtu.be/` (also with `?t=`), `/shorts/`, `/embed/`, `/live/`. A playlist or channel link has no video id and is not a video. A video linked on several pages, or twice on one page, is one video; its row remembers the first page (`found_in`, relative to the repo).

**When a video counts as known** (it is not added): it is the `video_id:` of a page in the docs (or the base id of an `id: <id>-gemini` page), it is the `doc_id` of a `job_items` row (in flight or done), it is already a YouTube or Gemini clip anywhere in the idea bucket (`inbox/`, `archive/`, `output/`, `failed/`, `duplicates/`), or it is already in the backlog. The output line counts all of these as "already have a page", so after the first import a rerun says `0 new` and counts the backlog rows there too.

**What a release writes.** For each of the oldest pending videos (oldest `found_at` first, then by id) a clip note `inbox/clippings/youtube source - <id>.md`, exactly:

```markdown
---
source: https://www.youtube.com/watch?v=<id>
tags: [clippings]
backfill: true
created: 2026-10-08
---
![](https://www.youtube.com/watch?v=<id>)
```

and the row becomes `released`. The note is written atomically and never over an existing file: when that note already exists, or the video is already a clip in the idea bucket, the row is marked `released` without a write (`N video(s) already had a clip note: marked released`). A note that cannot be written leaves its row `pending` for the next release (the error is only logged; the command still exits 0). **The idea bucket is not committed by the import**: the next `pipeline.run` stages the notes, and the next publish commits them (with the archive and working copies) like any capture.

**Low priority: new clips go first.** The marker `backfill: true` gives the item `origin = backfill`, and every job of that item (`youtube.fetch`, `llm.reason`, a fetch queued again) gets `BACKFILL_PRIORITY` (default `-10`); a normal clip's jobs stay at `0`. The queue takes the highest priority first, so a new clip never waits behind the backlog, and the YouTube gate still paces every fetch (a backfill fetch waits for the gap like any other). The marker stays on the archive and working copies (so a retry stays low priority) and never reaches the page. `catcher jobs list` shows the priority column; `catcher items list` shows the backfill items like any clip. One exception: an item that `catcher reconcile` rebuilds without an archive copy gets `origin = inbox`, so its jobs run at `0`.

**The cap is per rolling 24 hours.** `--limit N` compares N with **all** releases of the last 24 hours, whatever number you gave before: with 3 released, `--limit 3` again releases nothing (`daily allowance already used (3 per 24 hours)`), and `--limit 5` right after releases 2 more. So running it twice a day never goes over N, but raising N the same day releases the difference at once. `--release` uses `BACKFILL_DAILY_LIMIT` (default 10). A video marked `released` by a repair counts against the cap too. `--dry-run` with `--limit` or `--release` lists `would release <id>` and writes nothing.

**A staged start (recommended for the first big backfill).** A ban came from bursts before (see [YouTube IP bans and the queue](../idea-catcher-youtube-bans-and-queue-options/)), so start small and watch:

1. Day one: `catcher youtube import --dry-run`, then `catcher youtube import`, then `catcher youtube import --limit 5`. Let the worker (or `run pipeline`) work through them, and watch `catcher youtube gate` and the log for `YouTube is blocking us`.
2. No block after a day: `--limit 20` for a few days, then `--limit 50` (or set `BACKFILL_DAILY_LIMIT` and use `--release`).
3. **Raise `YOUTUBE_MIN_GAP_S` before you raise the limit**, not after a block. The gate allows **about 290 fetches a day at most** with the defaults (a 2-minute gap plus on average 2.5 minutes of jitter, an _estimate_), shared with the new clips, so a big backlog takes days by design. Each released video also costs one LLM call, like any clip (OpenAI; about $0.024 for a short video by the estimate in the YouTube page, more for a long one; the OpenAI key has its own budget cap).

There is **no daily automation**: nothing releases by itself. Run the release once a day by hand for now.

**A dead link is tried for about 3 days, then waits for you.** The cap limits releases, not retries: an old link is often a private or removed video, or one without captions, and its item is `deferred`. A scheduled run (`retry_deferred=true`) retries it once a day, but only until it becomes `stuck` (`STUCK_AFTER_DAYS`, 3): a **`stuck` backfill item is not retried any more**, so dead links do not cost a YouTube call every day for ever. The run logs `N stuck backfill item(s) not retried: requeue them by hand` (and lists them as `backfill_not_retried` in its result); `catcher items list --status stuck` shows them. Requeue one by hand with `catcher run pipeline --requeue NAME` (or `jobs add pipeline.run --param requeue=NAME`). A stuck item from the inbox (a normal clip) is still retried as before.

**Exit codes of `catcher youtube import`:** `0` done (also when a note could not be written); `1` `--channel` was stopped by the gate, a block, `YOUTUBE_OFFLINE` or a listing error (the channels listed before it are kept; with `--limit`/`--release` the release is skipped and it says so); `2` a folder is missing, a `--channel` URL is not a channel or playlist, `DATABASE_URL` is malformed, the database cannot be reached or has no tables (`run catcher db upgrade`). `catcher youtube backlog`: `0`, or `2` for the database.

### The channel listing (hand test only) {#backfill-channel}

`--channel URL` (repeatable) also lists the videos of a channel (`/@handle`, `/channel/ID`, `/c/name`, `/user/name`) or a playlist (`/playlist?list=...`) and adds the unknown ones to the backlog (`source channel`, `found_in` the URL as given). It is **the only part that calls YouTube**: one flat yt-dlp listing (no video page is opened) of at most `--max-videos` videos (default 50, at most 100: about 4 pages of 30), every request paced by `YOUTUBE_REQUEST_DELAY_S`, without retries, cookies or login. A listing must end well inside the gap its gate slot starts, so it never overlaps a worker's fetch or the next listing: `ceil(N / 30) x YOUTUBE_REQUEST_DELAY_S` must be at most half of `YOUTUBE_MIN_GAP_S` (at the defaults 10 s and 120 s: 6 pages, so the cap of 100 is what limits it); otherwise the command stops before any call (exit 2) and names the largest `--max-videos` the settings allow. Raising the request delay or lowering the gap therefore lowers what a listing may ask for. The channels of one command are listed one after another, each with its own gate slot, so the gate's gap separates them (the gap is counted from the start of a listing, which the rule above keeps safe). It goes through the gate: one gate slot per channel, a short gap is waited for (at most `YOUTUBE_WAIT_MAX_S`), a block or a longer gap stops it before any call (`nothing was listed`, exit 1), and a block during the listing opens the breaker like a failed fetch. `YOUTUBE_OFFLINE=1` refuses it. With `--dry-run` nothing is listed and no slot is taken. A bigger channel is listed over several runs, on different days; known videos are skipped.

**Not tried against YouTube yet** (the agents never run it). Try it by hand like this:

```bash
uv run catcher youtube gate                                                        # open?
uv run catcher youtube import --channel https://www.youtube.com/@SomeSmallChannel --max-videos 20 --dry-run
uv run catcher youtube import --channel https://www.youtube.com/@SomeSmallChannel --max-videos 20
uv run catcher youtube gate                                                        # still open, no block?
uv run catcher youtube backlog
```

Pick a **small** channel. Untried live: a bare channel URL gets `/videos` appended (without a tab yt-dlp lists the tabs, not the videos), and how many page requests one listing takes. If the result has no videos, try the URL with `/videos` yourself and look at the log.

**What was seen** (checked on 2026-10-08, offline: a throwaway Postgres, copies of the test repos with six extra pages of links in the docs copy, `YOUTUBE_OFFLINE=1`, no LLM key): the dry run printed `scanned 6 file(s), found 9 video(s), 3 already have a page, 6 new in the backlog` and wrote no row and no file; the import stored the 6; `--limit 3` wrote the three notes above and printed `released 3 video(s) into inbox/clippings/ (3 still pending)`; again `--limit 3` released nothing; `--limit 5` released 2. A normal clip dropped in afterwards and `catcher run pipeline`: the five backfill fetches had priority `-10`, the normal one `0`, and the normal one was taken first although it was queued last; offline the fetch defers the item at once (`YOUTUBE_OFFLINE is on and there are no saved facts`), so a fetch waiting in the queue for the gate was not seen here (the tests pin it). A backfill clip with saved facts went straight to `llm.reason` at `-10`. No page had a `backfill` key.

**Not built in B8:** a schedule that releases every day, a money budget beyond the cap and the per-backend budget block of B5, and the backfill origin for items that `catcher reconcile` rebuilds without an archive copy (they run at `0`).
