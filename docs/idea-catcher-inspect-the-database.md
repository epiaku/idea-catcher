---
title: "Idea Catcher: Inspect the Database"
linkTitle: "Idea Catcher: Inspect the DB"
description: "How to look into the Idea Catcher Postgres, run queries, make changes and export data: the tools compared, the safe way to reach the database on the Mac and on the Proxmox LXC, a read-only login, useful queries and the exports."
weight: 44
type: docs
---

**Written:** 2026-10-11
**Written for:** the owner, who wants to query the database, change rows and export data, on the Mac now and on the Proxmox LXC later.
**Status:** advice only. Nothing in this page is built into the repository yet; the one optional change (a `compose.tools.yaml`) is under [Not built yet](#not-built-yet).

## In short

- **Connect directly from the LAN, or over WireGuard when you are away, with a desktop tool.** Set `DB_BIND` on the LXC to its LAN IP and a real `DB_PASSWORD`. **DBeaver** does queries, edits and exports in one tool. An SSH tunnel to `127.0.0.1` is the fallback that needs no change on the server.
- **Use `psql` inside the `db` container** for quick checks, it needs nothing installed and works in every stack.
- **Browse with a read-only login, change data with the owner login.** Create the read-only one once (below).
- **Prefer the API and the `catcher` commands for changes** (requeue, publish, run). They keep the files, the items and the jobs consistent. Change rows by hand only to repair something, in a transaction.
- **A web tool in a container (pgAdmin, Adminer) is optional**, and only worth it if you want to browse from a phone or a machine without your tools installed.

## What is in the database

| Table | What it holds |
|---|---|
| `jobs` | The queue: one row per job (`type`, `status`, `params`, `result`, `error`, `attempts`, `run_after`, timings) |
| `job_items` | One row per document: its `status` (nine states), the LLM profile, backend and model, tokens, paths, `error`, `root_job_id` |
| `job_events` | A log per job or item (`level`, `message`, `data`) |
| `resources` | The YouTube gate and the blocks per backend (`next_allowed_at`, `blocked_until`, `streak`) |
| `schedules` | When each schedule fired last (`last_fired_at`) |
| `backfill_videos` | The old YouTube links of the backfill import |
| `alembic_version` | The migration the database is at |

Job types you will see: `pipeline.run`, `pipeline.preview`, `llm.reason`, `youtube.fetch`, `pipeline.publish`, `ideas.pull`. Job statuses: `queued`, `running`, `succeeded`, `failed`.

## The tools compared

| Tool | Runs where | Good for | Weak at |
|---|---|---|---|
| **`psql`** (in the `db` container) | the container | quick checks, scripts, `\copy` exports, works everywhere | no grid, no editing UI |
| **DBeaver** (free) | your Mac | queries, editing rows in a grid, exports to CSV, JSON, SQL and Excel, SSH tunnel built in, ER diagram | heavy to start |
| **TablePlus** (free tier, paid for more tabs) | your Mac | fast and light, SSH tunnel built in, good editing and export | the free tier limits open tabs |
| **VS Code extension** (SQLTools with the PostgreSQL driver) | your editor | queries next to the code, saved `.sql` files in the repo | exports and editing are basic |
| **pgAdmin** | a container or your Mac | full admin (roles, backups, query plans), web UI | large image, slow, a second login to protect |
| **Adminer** | a container | one small page, simple browse, query and export | fewer features, another web login to protect |

## Reaching the database

### On the Mac

`compose.yaml` publishes Postgres on `127.0.0.1:5432` (`DB_BIND`, `DB_PORT`). Connect a tool to host `localhost`, port `5432`, database `catcher`, user `catcher`, password `DB_PASSWORD` (the dev default is `catcher`). The local-test stack (`compose.local-test.yaml`) publishes **no port**, on purpose: use `psql` through Docker there:

```bash
docker compose -f compose.yaml -f compose.local-test.yaml exec db psql -U catcher catcher
```

### On the Proxmox LXC

You only reach the LXC from the home LAN, or from outside through the VPN (the Ubiquiti gateway with WireGuard). WireGuard makes a remote laptop behave like a LAN machine, so there are two ways in.

**1. Direct to the LXC's IP (the plan).** In `.env` on the LXC:

```
DB_BIND=<the LAN IP of the LXC>     # for example 192.168.1.50
DB_PASSWORD=<a real password>
```

Then connect the tool to `<lxc-ip>`, port `5432`, database `catcher`. The same address works at home and over WireGuard.

- **Bind to the LAN IP, never `0.0.0.0`.** If the LXC ever gets a second interface (a bridge, a VPN), `0.0.0.0` exposes the database there too.
- **Set the password before the first `up`.** Postgres applies `DB_PASSWORD` only when the data volume is first created; changing `.env` later breaks `migrate` and `worker`. On a database that already exists with the dev password, change it inside Postgres first (`ALTER ROLE catcher PASSWORD '...';`), then put the same value in `.env`.
- **Docker bypasses `ufw`.** A published port is opened through Docker's own iptables rules, so a `ufw deny 5432` usually does nothing. To limit the port to the LAN and WireGuard subnets, use the Proxmox firewall on the LXC or a rule on the Ubiquiti gateway.
- **The traffic is plain Postgres** (no TLS). On the home LAN that is acceptable, and over WireGuard it is encrypted anyway.
- **Browse with the read-only login** below; use the `catcher` login only for changes.
- **No port forwarding** to the internet, as in the architecture: the LAN and the VPN are the only ways in.

**2. SSH tunnel (the fallback).** Needs no change on the server, because the port stays on `127.0.0.1`. From your Mac:

```bash
ssh -L 5433:127.0.0.1:5432 root@<idea-catcher-lxc-ip>
```

Leave it open and point the tool at `localhost:5433` (5433 avoids a clash with a local Postgres). DBeaver and TablePlus can start the tunnel themselves (connection settings, tab "SSH"). Use it when you are somewhere without WireGuard, or when you do not want to change `DB_BIND`.

**3. `psql` on the LXC**, without any network setup:

```bash
ssh root@<idea-catcher-lxc-ip>
cd /opt/idea-catcher        # wherever compose.yaml lives
docker compose exec db psql -U catcher catcher
```

**4. A web tool on the LXC** (pgAdmin or Adminer), see [Not built yet](#not-built-yet). Only for LAN or VPN use, with a strong password.

## A read-only login for browsing

Run this once on the real database (as the owner), with a password of your own. Then connect your everyday tool as `inspector`, so a slip of the finger cannot change the queue:

```sql
CREATE ROLE inspector LOGIN PASSWORD 'choose-a-password';
GRANT CONNECT ON DATABASE catcher TO inspector;
GRANT USAGE ON SCHEMA public TO inspector;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO inspector;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO inspector;
```

Use the `catcher` login only when you mean to change something. Both logins connect the same way (direct, or through the tunnel). Keep both passwords out of Git.

## Making changes safely

- **Use the tools of the application first.** `catcher items`, `catcher jobs`, the requeue and run requests of the API: they change the database and the files together.
- **The database is the truth, the files are a mirror** (`stage` in the frontmatter of `output/`). A row changed by hand is not copied to the file until the worker's next change, and the other way round nothing flows back. After a manual repair, requeue the item (`catcher run pipeline --requeue NAME`) so the files and the row agree again. `catcher reconcile` goes the other way: it rebuilds missing item rows from the folders, for a lost or new database, and never changes a file.
- **Always in a transaction, with a count first:**

  ```sql
  BEGIN;
  UPDATE job_items SET status = 'deferred' WHERE calculated_name = '...';   -- one row?
  -- check the "UPDATE 1" count, SELECT the row, and only then:
  COMMIT;          -- or ROLLBACK;
  ```

- **Stop the worker (or know it is idle) before editing queue rows** (`jobs`): it claims and finishes jobs in the same table.
- **Take a dump first** when the change touches many rows (see [Backup and restore](#backup-and-restore)).

## Exports

| Need | How |
|---|---|
| One query to CSV | `psql`: `\copy (SELECT ...) TO 'jobs.csv' CSV HEADER`. DBeaver: run the query, then "Export data". |
| A whole table to JSON or SQL inserts | DBeaver: right-click the table, "Export Data". |
| The whole database | `docker compose exec -T db pg_dump -U catcher catcher > catcher-$(date +%F).sql` |
| One table | `... pg_dump -U catcher -t job_items catcher > job_items.sql` |

`\copy` writes on the machine where `psql` runs. Inside the container that is the container, so pipe it out instead: `docker compose exec -T db psql -U catcher catcher -c "\copy (SELECT ...) TO STDOUT CSV HEADER" > out.csv`.

## Backup and restore

The plan is a nightly `pg_dump` to a mounted folder plus the Proxmox backup of the LXC (see the [service architecture](../idea-catcher-service-architecture/)). The dump above is the same command; restore it into an empty database with:

```bash
docker compose exec -T db psql -U catcher catcher < catcher-2026-10-11.sql
```

Test a restore once on the Mac before you need it.

## Useful queries

How many jobs are there, per type and status?

```sql
SELECT type, status, count(*) FROM jobs GROUP BY type, status ORDER BY type, status;
```

The last failed jobs, with the reason:

```sql
SELECT type, params->>'calculated_name' AS document, error, finished_at
FROM jobs WHERE status = 'failed' ORDER BY finished_at DESC LIMIT 20;
```

What the chain did in the last hour (the order of the jobs):

```sql
SELECT created_at, type, status, params FROM jobs
WHERE created_at > now() - interval '1 hour' AND type IN ('ideas.pull', 'pipeline.run', 'pipeline.publish')
ORDER BY created_at;
```

Documents per state, and why the deferred ones wait:

```sql
SELECT status, count(*) FROM job_items GROUP BY status ORDER BY count(*) DESC;
SELECT calculated_name, stage_reason, stage_since FROM job_items WHERE status IN ('deferred', 'stuck') ORDER BY stage_since;
```

Tokens and models per profile:

```sql
SELECT llm_profile, llm_backend, llm_model, count(*) AS documents, sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out
FROM job_items WHERE llm_backend IS NOT NULL GROUP BY 1, 2, 3 ORDER BY documents DESC;
```

The log of one document:

```sql
SELECT e.ts, e.level, e.message FROM job_events e JOIN job_items i ON i.id = e.item_id
WHERE i.calculated_name = '...' ORDER BY e.ts;
```

The YouTube gate and the blocks:

```sql
SELECT name, next_allowed_at, blocked_until, streak, reason FROM resources;
```

## Not built yet

Nothing here is added to the repository. If you want a web tool next to the stack, this is the plan:

- A separate **`compose.tools.yaml`** with Adminer (small) or pgAdmin (full), added on the command line only when wanted (`-f compose.yaml -f compose.tools.yaml`), so it never runs in production by default.
- It joins the compose network and reaches the database as host `db`, port `5432`, so it also works in the local-test stack, where no port is published.
- Published on `127.0.0.1` of the Mac. On the LXC, bind it to the LAN address only with a strong password; with a desktop tool connected directly you can skip it.
