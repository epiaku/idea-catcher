---
title: "Idea Catcher: The Postgres Starting-Up Sequence"
linkTitle: "Idea Catcher: Postgres Startup Sequence"
description: "The sequence in which the Docker stack starts up: a temporary Postgres sets up the catcher database and stops, the real server starts, the one-time migrate container creates the tables, then the worker and the API start. Why the db log shows a shut down and a new start, and what a real problem looks like."
weight: 46
type: docs
---

When you start the stack for the first time (`docker compose -f compose.yaml -f compose.local-test.yaml up --build`, or the real `docker compose up`), the `db` log shows Postgres **starting, shutting down and starting again**. This looks like a crash. It is **normal**: it is how the official Postgres Docker image sets up an empty database.

## The log {#log}

This is the part of the log that raised the question (the `db-1` prefix is the container name; the dates are from one run):

```text
db-1  | waiting for server to start....2026-10-10 08:30:47.918 UTC [49] LOG:  starting PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2) on aarch64-unknown-linux-gnu, ...
db-1  | ... LOG:  listening on Unix socket "/var/run/postgresql/.s.PGSQL.5432"
db-1  | ... LOG:  database system was shut down at 2026-10-10 08:30:47 UTC
db-1  | ... LOG:  database system is ready to accept connections
db-1  |  done
db-1  | server started
db-1  | CREATE DATABASE
db-1  | /usr/local/bin/docker-entrypoint.sh: ignoring /docker-entrypoint-initdb.d/*
db-1  | waiting for server to shut down....... LOG:  received fast shutdown request
db-1  | ... LOG:  aborting any active transactions
db-1  | ... LOG:  shutting down
db-1  | ... LOG:  checkpoint starting: shutdown immediate
db-1  | ... LOG:  checkpoint complete: wrote 921 buffers (5.6%); ...
db-1  | ... LOG:  database system is shut down
db-1  |  done
db-1  | server stopped
db-1  | PostgreSQL init process complete; ready for start up.
```

## What happens, step by step {#steps}

1. **A temporary server starts** (`starting PostgreSQL 17.11`). The image has just run `initdb`, which creates the database files in an empty data folder. It starts a temporary Postgres to do its set-up work. This temporary server listens **only on a Unix socket inside the container**, not on the network, so no other container can connect to it.
2. **The set-up runs.** The image creates the `catcher` database (`CREATE DATABASE`, from the `POSTGRES_DB` setting) and looks for extra set-up scripts in `/docker-entrypoint-initdb.d/`. The line `ignoring /docker-entrypoint-initdb.d/*` only says there are none, which is fine: our migrations are run later by the `migrate` service, not by Postgres.
3. **The temporary server is stopped on purpose.** The image's own start-up script sends it a shutdown request (`received fast shutdown request`). The log shows an orderly stop: transactions aborted, a checkpoint written (`checkpoint complete`) and `database system is shut down`. This is not a crash. The image then says so itself: `PostgreSQL init process complete; ready for start up.`
4. **The real server starts.** Right after that line a second Postgres starts, and this one listens on the network (`listening on IPv4 address "0.0.0.0", port 5432`) and prints `database system is ready to accept connections`. This is the server the other containers use.

## When it happens {#when}

Only on a **fresh data volume**: the first `up` after the very first start, or after `docker compose ... down -v` (the `-v` deletes the volume with the data). When the data already exists (a plain restart, or `down` without `-v`), Postgres only does step 4 and the log has no set-up and no shutdown.

## Does it harm the other services? {#harm}

No. The `db` healthcheck (`pg_isready`) only succeeds once the **real** server answers. The `migrate` service waits for `db` to be healthy, and the `worker` and the `api` wait for `migrate` to finish. So nothing tries to connect while the temporary server runs, and the short stop and start has no effect on them.

## The `migrate` container {#migrate}

In `docker ps -a` you also see `catcher-local-migrate-1` (in the real stack `<project>-migrate-1`), with the status `Exited (0)`. It is a **one-time job container**: it creates or updates the tables in the database and then exits. It is not meant to keep running.

- **What it does.** It runs one command, `catcher db upgrade`, which applies the database migrations (Alembic): the `jobs`, `job_items`, `schedules`, `backfill_videos` and the other tables. On an empty database it creates everything; on an existing one it applies only what is missing.
- **Where it fits in the start order.** `db` starts first and has to become healthy. Then `migrate` runs and exits. The `worker` and the `api` start only after `migrate` has finished successfully (`service_completed_successfully`), so neither ever sees a database without tables.
- **What you see.** `Exited (0)`: exit code 0 means it succeeded, which is what you want. It does not show in `docker compose ps` (that lists only running services), but it does in `docker ps -a`. When the database is already up to date it writes no log lines, so `docker compose ... logs migrate` can be empty. It runs again each time you start the stack, which takes only a moment.
- **When to worry.** An exit code other than 0 (for example `Exited (1)`) means the migration failed, and then the worker and the API never start. Look at `docker compose ... logs migrate` for the error. It must not restart in a loop: the stack sets `restart: "no"` for it.
- **How we built it.** It skips the image's start-up script (`entrypoint: []`), because that script clones the repos and this container has no repos. It also has no repos volume, so your `tmp/ic` data does not affect it.

## What a real problem looks like {#problems}

| What you see | What it means |
| ------------ | ------------- |
| The same start-up lines again and again | `db` is in a restart loop. Look at the lines just before each restart for the reason |
| `FATAL` or `PANIC` lines | Postgres failed. Common causes: a full disk, a data folder from another Postgres version, a wrong password setting on an existing volume |
| `db` never reaches `(healthy)` in `docker compose -f compose.yaml -f compose.local-test.yaml ps` | The real server does not start or does not answer. Follow the log with `docker compose ... logs -f db` |
| `migrate` or the worker keep waiting | They wait for `db` to be healthy. Fix `db` first |
| `migrate` is `Exited (1)` (or another code that is not 0) | The migration failed, so the worker and the API do not start. See [the `migrate` container](#migrate) |

## How to check that it started well {#check}

```bash
# the db service should say "Up ... (healthy)"
docker compose -f compose.yaml -f compose.local-test.yaml ps

# the last lines of the db log: "ready to accept connections" and the 0.0.0.0 line
docker compose -f compose.yaml -f compose.local-test.yaml logs --tail 20 db

# the whole stack: expect 200 with "database": "ok" and "worker": "running"
curl -s -w " (status %{http_code})\n" http://127.0.0.1:8000/health
```

Use the second `-f` file only for the local test stack. For the real stack, use `docker compose ps` and `docker compose logs db` without it.

## See also {#see-also}

- [The First Local Test with the API](../idea-catcher-first-local-test/): the two-command test where you see this log.
- [How to Run Stage B](../idea-catcher-how-to-run-stage-b/#docker): the stack in Docker, the volumes, and what `down -v` deletes.
