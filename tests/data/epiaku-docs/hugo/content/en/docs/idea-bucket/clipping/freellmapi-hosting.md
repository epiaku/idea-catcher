---
title: "FreeLLMApi Hosting"
linkTitle: "FreeLLMApi Hosting"
description: "Where and how to host FreeLLMApi: home LXC on Proxmox, cloud options, persistent SQLite, exposing the home instance, Fly.io and home hardware."
weight: 10
type: docs
---

Summary of a Claude Code conversation (23–24 September 2026) about where to run **FreeLLMApi**: at home on Proxmox, or in the cloud so other apps can reach it. It records the options considered, the trade-offs, the final direction and the latest version of every code snippet.

## 📑 Table of Contents {#toc}

1. [Context & Goals](#context)
2. [Final Direction](#final-direction)
3. [Hosting Options for FreeLLMApi](#hosting-options)
4. [FreeLLMApi Source Review](#freellmapi-review)
5. [Persistent SQLite Options](#persistence)
6. [Database Model & Azure Tables Feasibility](#db-model)
7. [Exposing the Home FreeLLMApi](#exposing-home)
8. [Fly.io Deployment](#flyio)
9. [Running on the H4](#running-h4)
10. [Home Hardware (H4, N2, Windows/Mac)](#hardware)
11. [Next Steps](#next-steps)

---

## 🎯 Context & Goals {#context}

**Current setup**

- **FreeLLMApi** ([tashfeenahmed/freellmapi](https://github.com/tashfeenahmed/freellmapi)) runs in an LXC on **Proxmox**, on an **ODROID H4**, in the home network behind a **UniFi fiber gateway**.
- FreeLLMApi is an OpenAI-compatible proxy that routes requests to free-tier LLM providers (Groq, Gemini, OpenRouter, …) and handles fallback and rate limits. It keeps provider keys, stats and the unified API key in **SQLite**.

**Goals**

- Serve the [idea catcher pipeline](../../gemini/idea-catcher-pipeline/), which runs a few small batch jobs a day.
- Future web apps and agents may also need an LLM API.
- **Keep the home network closed:** no inbound access from cloud services into the LAN.
- Prefer free or very cheap services. The team has Azure Container Apps (ACA) experience.

---

## 🧭 Final Direction {#final-direction}

The conversation went from "FreeLLMApi in the cloud" to a simpler **home-first** setup:

```text
ODROID H4 (Proxmox)
 ├── LXC: FreeLLMApi            ← LAN only, for dev work (Aider, local tools)
 └── LXC: idea catcher pipeline ← calls FreeLLMApi over the LAN (see the pipeline page)
```

| Decision | Choice | Why |
|---|---|---|
| Where FreeLLMApi runs | **LXC on the ODROID H4**, LAN only | Always on, already runs Proxmox; the pipeline also runs at home, so all traffic stays inside the LAN or goes outbound |
| Cloud FreeLLMApi | **Not needed now.** If a web app needs a shared API later: **Fly.io** with a volume (~$1–3.50/month) | Simplest persistent-SQLite option |

{{% alert title="Key lesson" color="info" %}}
FreeLLMApi is a long-running, single-instance app with SQLite. Scale-to-zero serverless platforms (ACA, Cloud Run, Fargate, App Runner) wipe the disk, which is why every cloud option needed workarounds. A normal disk (home server, VM, or Fly.io volume) avoids all of them.
{{% /alert %}}

---

## ☁️ Hosting Options for FreeLLMApi {#hosting-options}

### Overview of all options discussed

| Option | Persistent SQLite? | Approx. cost/month | Notes |
|---|---|---|---|
| **GitHub Actions** service container | ❌ per run | Free (2,000–3,000 min/month on private repos) | Fine for batch jobs; not a shared API |
| **Azure Container Apps** (apps + jobs) | ❌ without workarounds | Free grant (180k vCPU-s, 360k GiB-s) | Scale to zero, internal ingress, Key Vault refs; SQLite needs Litestream or a backup file |
| **Google Cloud Run** (+ Scheduler) | ❌ without workarounds | Free tier | Same model as ACA; supports sidecars |
| **Oracle Cloud Always Free** VM | ✅ | €0 | Generous ARM VM; fiddly signup; idle accounts can be reclaimed |
| **Hetzner CX22** + Docker Compose | ✅ | ~€4 | Same setup as Proxmox; EU; you manage the VM |
| **Fly.io** + volume | ✅ | ~$1–3.50 | No free tier; auto-stop; managed HTTPS |
| **AWS Lightsail** instance | ✅ | ~$5 (IPv4 included) | Simplest on AWS |
| **AWS EC2** t4g.nano/micro + EBS | ✅ | ~$3 + ~$1 disk + $3.60 public IPv4 | New accounts get credits, not the old 12-month free tier |
| **AWS Fargate + Litestream → S3** | ✅ (replicated) | ~$9 + ALB ~$16 | Serverless, but more setup |
| AWS Fargate + EFS | ⚠️ | ~$9 | NFS: SQLite WAL not supported |
| AWS App Runner / Lightsail Containers | ❌ | – | No persistent storage |
| Render / Railway / Koyeb | Mostly paid now | ~$2–5 | No advantage here |
| Cloudflare Containers | – | Needs $5 Workers plan | Not worth it |

### Azure Container Apps design (reference)

Explored in detail because of existing ACA experience:

```text
ACA environment (consumption, free grant)
├── freellmapi          Container App, scale 0..1, key auth
├── idea-summarizer     Container App JOB (cron), calls freellmapi internally
└── future web apps     Container Apps, call freellmapi internally
```

- **Internal ingress:** apps in the same environment call `http://freellmapi` privately.
- **`maxReplicas: 1`:** FreeLLMApi tracks rate limits in SQLite/memory; multiple replicas would double-count.
- **`minReplicas: 0`** ≈ free, with a few seconds of cold start; `minReplicas: 1` ≈ €2–5/month.
- **Cost traps:** use GHCR instead of ACR (~€5/month); cap Log Analytics or use `--logs-destination none`.
- **Security:** API key, ingress IP allow-list, internal-only ingress, optionally Entra/Easy Auth.

```bash
az containerapp env create -n epiaku-env -g epiaku-rg -l westeurope --logs-destination none
```

Why it was dropped: FreeLLMApi needs persistent SQLite, and ACA's filesystem is wiped on scale-down (see [Persistent SQLite Options](#persistence)).

---

## 🔍 FreeLLMApi Source Review {#freellmapi-review}

The repo was cloned and read to check whether the container can be configured statelessly from environment variables, e.g. from Key Vault. The first review used commit `1346b7d` (v0.11.1). It was re-checked against **v0.12.0** (commit `e4a47f2`, 24 September 2026); see [What changed in v0.12.0](#v0120-changes).

### What can be injected at startup

`server/src/services/declarative-config.ts` applies **`FREEAPI_CONFIG_JSON`** (inline) or **`FREEAPI_CONFIG_PATH`** (file) on every boot, after migrations and **before the HTTP listener starts**. Applying it again gives the same result.

| Setting | v0.11.1 | v0.12.0 |
|---|---|---|
| Provider API keys (`keys[]`: platform, key, label, enabled) | ✅ config | ✅ config |
| Custom OpenAI-compatible providers (`customProviders[]`) | ✅ config | ✅ config |
| Model edits: limits, ranks, enabled (`models[]`) | ✅ config | ✅ config |
| Fallback chain order (`fallback[]`) | ✅ config | ✅ config |
| Routing strategy / weights / key selection (`routing`) | ✅ config | ✅ config |
| `ENCRYPTION_KEY` (required when `NODE_ENV=production`) | ✅ env | ✅ env |
| Rate limits, timeouts, caps, cache, retention, etc. | ✅ env | ✅ env (see `.env.example`) |
| **Dashboard admin account** | ❌ UI + setup code in logs | ✅ **config (`admin`)** |
| Premium license (freellmapi.co) | – | ✅ config (`license`) |
| **Unified API key** (`freellmapi-…`) | ❌ | ❌ random per new DB (`server/src/db/index.ts:233`), no override |

Example config (v0.12.0):

```json
{
  "admin": { "email": "ops@epiaku.com", "password": "min-8-chars" },
  "keys": [
    { "platform": "groq",   "key": "gsk_...", "label": "main" },
    { "platform": "google", "key": "AIza...", "label": "main" }
  ],
  "routing": { "strategy": "balanced" }
}
```

- **Check platform IDs** against `server/src/providers/index.ts`. An unknown platform stops the config from being applied.
- **The JSON now contains a plaintext admin password.** Store the whole config as one Key Vault secret, protected like `ENCRYPTION_KEY`.

### What changed in v0.12.0 {#v0120-changes}

**1. Admin account from config (#1291).**
- **No setup screen:** the `admin` entry creates the first dashboard account before the server accepts connections, so a fresh container never shows the first-run setup screen and no setup code is created. That removes the risk of someone claiming a freshly exposed Fly.io/ACA instance before you do.
- **Only when no account exists:** once an account exists, the entry is ignored with a warning, so config can never take over an already claimed server.
- **Stateless container:** the DB is empty on every boot, so the account is recreated each time from the same Key Vault secret.
- **Persistent DB** (home, Fly.io volume, Litestream): changing the password in the config has no effect after the first boot. Change it in the dashboard instead.

**2. `license` entry.** Activates a Premium key ($19/year, live signed model catalog) in the background. It needs outbound access to freellmapi.co and is irrelevant without Premium.

**3. `freellmapi keys add | list | remove | test` CLI (#1276).** Manages provider keys **over the HTTP API of a running server**, authenticated with a **dashboard session token** (`FREELLMAPI_DASHBOARD_TOKEN`), not the unified key. Useful for scripting key changes against the H4 instance. It is **not** a startup injection mechanism; the declarative config remains that.

**4. Security fixes relevant here.**
- **#1287:** a reverse proxy on the same host (Caddy/nginx) can no longer skip the setup code.
- **#1286:** login brute-force throttling now covers all paths.

**5. Unchanged:**

| Area | Status |
|---|---|
| Unified API key | Still random on every new DB, still no env override |
| SQLite, WAL mode, synchronous `Db` interface | Unchanged, so the Azure Tables analysis stands |
| Built-in backup (no `x-ms-blob-type` header) | Unchanged |
| Litestream approach | Unaffected |
| Migrations | One new (38 total): request history now records the exact model entry, backfilled on first start. Back up the DB before upgrading |

{{% alert title="Stateless status" color="info" %}}
On a stateless container, the **unified API key is now the only thing that changes on every cold start**. A small upstream PR (an env override such as `FREEAPI_UNIFIED_API_KEY` in `getUnifiedApiKey()`) would make FreeLLMApi fully stateless from a single Key Vault secret; only stats and quota counters would be lost between restarts.
{{% /alert %}}

### Other relevant findings

- **SQLite runs in WAL mode**, hard-coded (`server/src/db/index.ts:82`).
- **Built-in encrypted backup:** `FREEAPI_DB_BACKUP_PATH` / `FREEAPI_DB_BACKUP_URL`. It restores at startup if the DB is missing, then backs up every `FREEAPI_DB_BACKUP_INTERVAL_MS` (default 5 min). There's **no backup on shutdown**, and the file is overwritten in place, not written atomically.
- **Azure Blob won't work as a backup URL:** uploads are a plain `PUT` without the `x-ms-blob-type` header that Blob Storage requires. Restore with a SAS GET would work.
- **The unified key is read from the DB on every request.** That makes a startup wrapper possible.

### Stateless workaround for the unified key (still needed in v0.12.0, not used)

```sh
#!/bin/sh
node server/dist/index.js &
PID=$!
until node -e "fetch('http://127.0.0.1:3001/api/ping').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"; do sleep 0.5; done
node -e "
  const db = require('better-sqlite3')(process.env.FREEAPI_DB_PATH || '/app/server/data/freellmapi.db');
  db.prepare(\"UPDATE settings SET value=? WHERE key='unified_api_key'\").run(process.env.UNIFIED_API_KEY);"
wait $PID
```

A cleaner fix would be a small upstream PR adding `FREEAPI_UNIFIED_API_KEY`. That PR could also add the `x-ms-blob-type` header for Azure Blob backups.

### ACA + Key Vault wiring (reference)

```bash
az containerapp secret set -n freellmapi -g epiaku-rg --secrets \
  "config-json=keyvaultref:https://epiaku-kv.vault.azure.net/secrets/freellmapi-config,identityref:system" \
  "encryption-key=keyvaultref:https://epiaku-kv.vault.azure.net/secrets/freellmapi-enc-key,identityref:system" \
  "unified-key=keyvaultref:https://epiaku-kv.vault.azure.net/secrets/freellmapi-unified-key,identityref:system"
```

```bash
az containerapp update -n freellmapi -g epiaku-rg --set-env-vars \
  FREEAPI_CONFIG_JSON=secretref:config-json ENCRYPTION_KEY=secretref:encryption-key \
  UNIFIED_API_KEY=secretref:unified-key NODE_ENV=production \
  FREEAPI_BLOCK_PRIVATE_PROVIDER_URLS=true FREELLMAPI_UPDATE_CHECK=off
```

---

## 💾 Persistent SQLite Options {#persistence}

| Approach | Verdict | Reason |
|---|---|---|
| Live DB on **Azure Files (SMB)** / **EFS (NFS)** | ❌ | SQLite WAL needs shared memory, which network file systems don't support; SMB locking is unreliable; ACA revision overlap means two writers |
| Azure Files **NFS** (Premium) | ❌ | WAL still unsupported; needs Premium + VNet (~€15+/month) |
| **Local DB + backup file on Azure Files** | ⚠️ Works | Uses FreeLLMApi's built-in backup; loses up to one backup interval; a partly written backup blocks startup |
| **Litestream → S3 / Azure Blob** | ✅ Best serverless option | Uploads changes every sync interval (1 s default, 10 s is fine); restores on startup. Explained below |
| **Normal disk** (home, VM, Fly volume) | ✅ Simplest | No workarounds |

### Backup file on Azure Files (ACA)

Key settings: mount the share at `/mnt/backup` with `mountOptions: "uid=1000,gid=1000,dir_mode=0700,file_mode=0600"` (uid 1000 = the `node` user in the image), then set:

```yaml
env:
  - { name: NODE_ENV, value: production }
  - { name: ENCRYPTION_KEY, secretRef: encryption-key }        # must never change
  - { name: FREEAPI_CONFIG_JSON, secretRef: config-json }
  - { name: FREEAPI_DB_BACKUP_PATH, value: /mnt/backup/freellmapi.db.backup }
  - { name: FREEAPI_DB_BACKUP_INTERVAL_MS, value: "60000" }
  - { name: REQUEST_ANALYTICS_RETENTION_DAYS, value: "14" }
  - { name: REQUEST_ANALYTICS_MAX_ROWS, value: "20000" }
```

Turn on share soft delete and snapshots so you can recover from a corrupt backup.

### What Litestream is

[Litestream](https://litestream.io) is a small open-source program that **continuously copies a SQLite database to cloud storage** (Azure Blob Storage, S3, Google Cloud Storage, or a local folder). It runs next to the app in the same container, and the app itself doesn't change.

- **Every change is saved within seconds** (default sync interval 1 s), rather than waiting for a periodic backup.
- **Automatic restore:** when a container starts with an empty disk, it downloads the latest copy before the app starts.
- **The app still thinks it's on a normal local disk** and keeps using SQLite as usual.

That fits ACA's scale-to-zero: the container's disk is empty on every start, and Litestream refills it from Blob Storage within seconds.

### How it works

SQLite in WAL mode, which FreeLLMApi uses, writes every change to a `-wal` file first and folds it into the main `.db` file later (a "checkpoint").

```text
FreeLLMApi ──writes──▶ freellmapi.db-wal ──▶ freellmapi.db      (local disk, fast)
                              │
                     Litestream reads new WAL pages every sync-interval
                              ▼
               Azure Blob container "freellmapi"
               ├── snapshot (full DB copy, every 24 h by default)
               └── LTX files (small change sets, merged over time)
```

- **It controls checkpointing.** Litestream holds a long-running read transaction so nothing else can checkpoint, copies each new change, then does the checkpoint itself. That's why **the app must not run its own checkpoints**. In FreeLLMApi only the built-in backup feature does that (`lib/db-backup.ts:251`), so leave `FREEAPI_DB_BACKUP_*` unset.
- **Changes are uploaded as small "LTX" files** with sequential transaction IDs, merged in the background. A full snapshot is written every 24 hours.
- **Restore** downloads the latest snapshot and replays the later LTX files in order.
- **v0.5 changed the format** (LTX instead of the older "generations"), so use v0.5 docs and examples and pin the version.

### Container lifecycle on ACA

| Moment | What happens |
|---|---|
| **Cold start** (first request after scale to zero) | `litestream restore` downloads the DB (a few MB, 1–3 s), then `litestream replicate` starts FreeLLMApi |
| **Running** | FreeLLMApi reads and writes the local SQLite at full speed; Litestream uploads only when something changed |
| **Scale to zero / stop** | ACA sends SIGTERM and Litestream stops FreeLLMApi. Whether Litestream does a final upload on shutdown isn't documented, so test it (see below) |
| **First deploy ever** | No copy exists yet; `-if-replica-exists` skips the restore and FreeLLMApi creates a fresh DB |

### Litestream wrapper image

```dockerfile
# Dockerfile: FreeLLMApi + Litestream
ARG FREELLMAPI_TAG=v0.12.0
ARG LITESTREAM_TAG=0.5.17

FROM litestream/litestream:${LITESTREAM_TAG} AS litestream

FROM ghcr.io/tashfeenahmed/freellmapi:${FREELLMAPI_TAG}
# Statically linked binary, so it runs on the Debian-based FreeLLMApi image.
COPY --from=litestream /usr/local/bin/litestream /usr/local/bin/litestream
COPY litestream.yml /etc/litestream.yml
COPY --chmod=755 start.sh /usr/local/bin/start.sh
CMD ["/usr/local/bin/start.sh"]
```

```yaml
# litestream.yml (v0.5 format: one `replica` per database; ${VARS} are expanded from env)
#   local test : LITESTREAM_REPLICA_URL=file:///replica
#   Azure      : LITESTREAM_REPLICA_URL=abs://epiakufreellm@freellmapi/db  (+ managed identity)
logging:
  level: info

dbs:
  - path: /app/server/data/freellmapi.db
    replica:
      url: ${LITESTREAM_REPLICA_URL}
      sync-interval: ${LITESTREAM_SYNC_INTERVAL}
```

```sh
#!/bin/sh
# start.sh: restore if the local disk is empty (cold start), then run FreeLLMApi
# as a child of Litestream so every change is replicated.
set -e
DB=/app/server/data/freellmapi.db
litestream restore -if-db-not-exists -if-replica-exists "$DB"
exec litestream replicate -exec "node server/dist/index.js"
```

- **The base image's entrypoint still runs first.** The Dockerfile only sets `CMD`, so the image's `ENTRYPOINT` (`docker-entrypoint.sh`) fixes data-dir ownership and starts `start.sh` as the unprivileged `node` user.
- **`-exec` makes Litestream the parent process.** It starts FreeLLMApi, replicates while it runs, and exits when FreeLLMApi exits.
- Both images are published for **amd64 and arm64**. Check `litestream restore -h` for the flags in the pinned version.

### Running it on ACA with Azure Blob Storage

**1. Storage** (a dedicated blob container):

```bash
az storage account create -n epiakufreellm -g epiaku-rg -l westeurope --sku Standard_LRS --kind StorageV2
```

```bash
az storage container create --account-name epiakufreellm -n freellmapi --auth-mode login
```

**2. Access without a key (managed identity):**

```bash
az containerapp identity assign -n freellmapi -g epiaku-rg --system-assigned
```

```bash
az role assignment create --role "Storage Blob Data Contributor" \
  --assignee "$(az containerapp show -n freellmapi -g epiaku-rg --query identity.principalId -o tsv)" \
  --scope "$(az storage account show -n epiakufreellm -g epiaku-rg --query id -o tsv)"
```

- **The role must be *Storage Blob Data Contributor*.** Owner or Contributor don't grant access to blob data. Role assignments can take up to about 10 minutes to apply.
- **Alternative:** an account key in the ACA secret `LITESTREAM_AZURE_ACCOUNT_KEY`. Managed identity is cleaner because there's no key to leak.

**3. Container App settings:**
- `minReplicas: 0`, **`maxReplicas: 1`**, **single revision mode**.
- Env: `NODE_ENV=production`, `ENCRYPTION_KEY` (Key Vault, **never change it**), `LITESTREAM_REPLICA_URL=abs://epiakufreellm@freellmapi/db`, `LITESTREAM_SYNC_INTERVAL=10s`, optionally `FREEAPI_CONFIG_JSON`. **Don't** set `FREEAPI_DB_BACKUP_*`.
- **No volume mount:** the DB lives on the container's temporary disk; Blob holds the durable copy.

**4. Check the restore:** add a key or send a request, restart the revision, and confirm the data is still there. To inspect the Azure copy locally (after `az login`):

```bash
litestream restore -o ./.tmp/freellmapi-copy.db abs://epiakufreellm@freellmapi/db
```

### Choosing the sync interval

A longer interval such as **10 s, or even 30–60 s**, is fine for FreeLLMApi:

- **Normal scale-down loses nothing.** ACA scales down only after a quiet period (5 minutes by default), and the interval has long caught up by then. Writes in that quiet period are background housekeeping (health checks, quota observations, logs), which don't matter if lost.
- **A crash can lose up to one interval.** If the container is killed without warning (out-of-memory, node crash), at most the last interval of writes is lost. Those are mostly stats, rarely config.
- **The open question is the shutdown flush.** If Litestream doesn't upload on SIGTERM, a config change could be lost if the container stops within the interval after it. The local test below answers that.
- **Longer intervals mean fewer Blob write operations**, which lowers the (already small) cost.

### Caveats

- **Exactly one writer.** Two containers writing to the same copy in Blob Storage will corrupt its history. With `maxReplicas: 1` this is handled during normal running.
- **Deploys can briefly run two containers.** Even in single revision mode, ACA starts the new revision before stopping the old one. The safest deploy is to **deactivate the old revision first** (`az containerapp revision deactivate …`), then activate the new one. That costs a few seconds of downtime.
- **AWS equivalent:** on ECS, set desired count 1 and minimum healthy 0%, and use an `s3://` replica URL.
- **Cost:** storage is only a few MB. The cost is mostly write operations, which only happen while data changes: cents to about a dollar a month with scale-to-zero.
- **Not tested with FreeLLMApi yet.**

### Testing it locally with Docker Compose

The concept can be tested on any machine with Docker (OrbStack or Docker Desktop on a Mac, or an LXC on the H4) without Azure:

- **Blob simulator:** a `file:///replica` replica URL, mapped to a local folder, replaces Azure Blob. Only the replica URL changes compared with Azure.
- **Simulated scale-to-zero:** **no volume** for `/app/server/data`, so every `docker compose down` destroys the container's disk, like an ACA replica scaling to zero. Only `./replica` survives.
- **`stop_grace_period: 30s`** mimics ACA's termination grace period.

```yaml
# docker-compose.yml
services:
  freellmapi:
    build: .
    environment:
      NODE_ENV: production
      ENCRYPTION_KEY: ${ENCRYPTION_KEY}
      LITESTREAM_REPLICA_URL: file:///replica
      LITESTREAM_SYNC_INTERVAL: ${SYNC_INTERVAL:-10s}
    ports:
      - "127.0.0.1:3001:3001"
    volumes:
      - ./replica:/replica            # blob simulator; NO volume for /app/server/data
    stop_grace_period: 30s
```

Scenarios worth checking:

| # | Scenario | What it shows |
|---|---|---|
| 1 | Write, wait longer than the interval, `down`, `up` | The unified API key and data survive a cold start |
| 2 | Write, then `down` (SIGTERM) 2 s later with a 60 s interval | Whether Litestream flushes pending changes on shutdown |
| 3 | Write, then `docker compose kill -s KILL` inside the window | Worst-case loss: up to one sync interval |
| 4 | `litestream restore -o /tmp/check.db file:///replica` | Restoring a copy for inspection works |

A ready-made version of this setup (Dockerfile, config, compose file and a `test.sh` that runs the four scenarios) is in `.tmp/litestream-test/` in this repo. That folder is gitignored and hasn't been run yet.

---

## 🗄️ Database Model & Azure Tables Feasibility {#db-model}

Question: is FreeLLMApi's database a few simple tables, or a relational model? And could the code be changed to use **Azure Table Storage** instead of SQLite, so it can run statelessly on ACA? This is an analysis only (commit `1346b7d`, re-checked on v0.12.0: the data layer is unchanged); nothing was implemented.

{{% alert title="Conclusion" color="info" %}}
The database is **moderately complex, and a lot of logic depends on it**. An Azure Table Storage version is technically possible but means rebuilding the data layer and maintaining a fork of a fast-moving project. **Not recommended.** Litestream with Azure Blob Storage gets FreeLLMApi onto ACA with no code changes.
{{% /alert %}}

### Size and shape

| Metric | Value |
|---|---|
| Tables | **~30** (31 `CREATE TABLE`, one is a temporary copy used while rebuilding a table) |
| Migrations | **38** in about 9 months as of v0.12.0 (37 with 3,905 lines at v0.11.1): the schema changes almost every week |
| Declared foreign keys | **6** (5 with `ON DELETE CASCADE`), enforced (`foreign_keys = ON`) |
| Links by convention (no FK) | Many: `key_id` in 7+ tables points to `api_keys`; `(platform, model_id)` links `requests`, `models`, `model_overrides` and the tombstone tables |
| Indexes | 28 |
| Triggers | 1: every insert into `requests` updates `key_monthly_usage` |
| SQL queries (`db.prepare`) | **518** across **69 files** (server code, tests excluded) |
| Transactions (`db.transaction`) | 39 |
| SQL features used | 62 JOINs (47 LEFT JOIN), 33 GROUP BY, 139 aggregates (COUNT/SUM/AVG/MAX), subqueries, UPSERTs (`ON CONFLICT`), a window function, `json_extract`, date functions |

### Table groups

| Group | Tables | Access pattern |
|---|---|---|
| **Configuration** (~18) | `models`, `api_keys`, `fallback_config`, `profiles`, `profile_models`, `model_overrides`, `catalog_model_tombstones`, `custom_model_tombstones`, `embedding_models`, `media_models`, `quirks`, `quirk_targets`, `settings`, `users`, `sessions`, `url_tokens`, `client_profiles`, `playground_conversations`, `backups` | Relational: joins between models, fallback chain, profiles and keys |
| **Hot runtime state** (~5) | `rate_limit_usage`, `rate_limit_cooldowns`, `provider_quota_state`, `idempotency_claims`, `response_cache` | Read and written **on every proxied request**; time-window counts and sums |
| **Telemetry** (~6) | `requests`, `request_attempts`, `request_hourly`, `key_monthly_usage`, `provider_quota_observations`, `server_logs` | Many small inserts plus dashboard analytics (GROUP BY, percentiles, averages) |

### Relationships

```text
models ─┬─< fallback_config            (FK)
        └─< profile_models >── profiles (FK, cascade both sides)
users ──< sessions                      (FK, cascade)
requests ──< request_attempts           (FK, cascade)
quirks ──< quirk_targets                (FK, cascade)

api_keys ◁┈ models.key_id, requests.key_id, rate_limit_*.key_id,
            provider_quota_*.key_id, key_monthly_usage.key_id   (convention only, no FK)
```

The most-joined tables are `models` (17 joins), `api_keys` (17) and `fallback_config` (12). Those joins are how the router works out which model and key to use next.

### Why Azure Table Storage would be a large refactor

1. **The whole codebase calls the database synchronously.** It uses `better-sqlite3`, whose `get()`, `all()` and `run()` return results immediately, and the app's `Db` interface (`server/src/db/types.ts`) has the same shape. Azure Table Storage, like any network database, is **async over HTTP**. All 518 call sites, and every function above them (router, rate limiter, middleware such as `getUnifiedApiKey()`), would have to become `async`. That change runs through a 52,000-line server. Moving to Postgres or Cosmos DB would hit the same barrier.
2. **The queries don't fit a key-value store.** Azure Table Storage can only look up efficiently by `PartitionKey` and `RowKey`. It has **no JOIN, COUNT, SUM, GROUP BY or ORDER BY**, no foreign keys or triggers, and transactions only within one partition (up to 100 operations):
   - **The rate-limit hot path** (`services/ratelimit.ts`) runs `SELECT COUNT(*)` and `SUM(tokens)` over a sliding time window per platform, model and key. On Tables you'd have to fetch all the matching rows and count them in Node, or keep counter rows updated with ETag optimistic concurrency.
   - **The router** joins `models`, `fallback_config`, `profile_models` and `api_keys` to rank candidates. That would become several fetches merged in memory.
   - **Analytics** (`routes/analytics.ts`) does GROUP BY, AVG and percentile queries over `requests`. That would need pre-computed aggregate tables, or fetching and computing in memory.
   - **Cascading deletes, the trigger and 39 transactions** would all have to be re-implemented in application code.
3. **Each request gets slower.** SQLite answers in microseconds. Each Azure Table call takes about 5–30 ms, and the router and rate limiter make several calls per proxied request. That adds noticeable latency before the request even reaches the provider.
4. **Keeping a fork current is the biggest cost.** 38 migrations in 9 months, with commits almost daily, means porting every upstream schema change and new query to your Table Storage version, indefinitely.

**Effort estimate:** several weeks for a first working version (a new data layer, the async rewrite, re-implemented aggregations and tests), plus ongoing work to port every upstream change.

### Better ways onto Azure ACA

| Option | Code change | Cost | Notes |
|---|---|---|---|
| **Litestream → Azure Blob Storage** | **None** (wrapper image) | Cents per month (Blob) | Changes uploaded every sync interval; restored at startup. Still exactly one replica. Full walkthrough in [Persistent SQLite Options](#persistence) |
| FreeLLMApi's own backup file on Azure Files | None | Cents | Can lose up to one backup interval; a half-written backup can block startup |
| Azure Table Storage / Cosmos DB | **Very large** (async rewrite + data layer) | Low | Not recommended; you'd maintain a fork |
| Postgres (Azure Flexible Server) | Large (async rewrite + SQL dialect) | ~€12–15/month (B1ms) | Only worth it if upstream adds Postgres support themselves |
| libSQL / Turso "embedded replicas" | Possibly small | Free tier | Keeps a local SQLite file synced with a remote copy, via a package that claims better-sqlite3 compatibility. **Not verified** that writes and the app's synchronous calls work this way. Worth a spike only if Litestream falls short |

**Recommendation:** if FreeLLMApi ever needs to run on ACA, use **Litestream with Azure Blob Storage**: max replicas 1, single revision mode, and FreeLLMApi's own backup turned off. The code stays as upstream ships it, and upgrades are just a new image tag. A Table Storage rewrite only makes sense if upstream adds a pluggable storage layer, which the synchronous design suggests isn't planned. That could be raised as an issue upstream.

---

## 🌐 Exposing the Home FreeLLMApi {#exposing-home}

Idea: keep one FreeLLMApi on the H4 and let cloud apps reach it, e.g. through an nginx proxy in ACA over the UniFi VPN.

{{% alert title="Catch" color="warning" %}}
ACA containers have no `NET_ADMIN` or `/dev/net/tun`, so nginx can't use a kernel VPN. You'd need a **userspace** client such as **wireproxy** (WireGuard → local port) or **Tailscale in userspace mode** as a sidecar. Site-to-site IPsec to an Azure VPN Gateway costs ~€25–130+/month.
{{% /alert %}}

```text
ACA app (scale 0..1)
 ├── nginx       :80  → only /v1/* → localhost:3001
 └── wireproxy        localhost:3001 ──WireGuard──▶ UniFi gateway ──▶ LXC 192.168.x.y:3001
```

### Risks and mitigations

| Risk | Mitigation |
|---|---|
| A cloud container gets a VPN into the LAN | UniFi firewall: VPN client may reach **only** LXC IP:3001; separate VLAN |
| Inbound UDP port on the gateway | WireGuard stays silent to anyone without a valid key (low risk) |
| Home outage takes down cloud apps | Fine for batch jobs, weak for customer-facing apps |
| Dynamic IP / CGNAT | UniFi DDNS; check for a public IP |
| Dashboard exposed through the proxy | nginx forwards only `/v1/` |
| Per-IP rate limit (`PROXY_RATE_LIMIT_RPM`=120) sees one IP | Raise it, or use `TRUST_PROXY` + `X-Forwarded-For` |
| Streaming / long responses | `proxy_buffering off; proxy_read_timeout 300s;` (ACA ingress ~240 s) |

### Alternatives compared

| | UniFi WireGuard + ACA proxy | Tailscale + ACA proxy | **Cloudflare Tunnel** |
|---|---|---|---|
| Inbound port at home | Yes (UDP) | No | No |
| Cloud has network-level access | Yes (limit with firewall) | Yes (limit with ACLs) | **No, only one HTTP service** |
| Proxy container in Azure | Needed | Needed | **Not needed** |
| Cost | Free | Free | Free |

Cloudflare Tunnel (`cloudflared` next to FreeLLMApi, protected by a **Cloudflare Access service token** and exposing only `/v1/*`) was the best of these. In the end, the pipeline moved to the home network instead.

---

## 🪰 Fly.io Deployment {#flyio}

Kept as the preferred option **if** a cloud-hosted, shared FreeLLMApi is needed later.

### Pricing

- **No free tier** for new customers, only a one-time trial (2 VM hours or 7 days).
- **Pay-as-you-go** by default. "Compute reservations" ($36/year → $5/month credit that doesn't roll over) are **optional** and not worth it here.
- Stopped machines cost only root-filesystem storage ($0.15/GB per 30 days stopped).

| Item (Amsterdam, shared-cpu-1x 512 MB) | 8 h/day | Always on |
|---|---|---|
| Machine CPU/RAM (~$0.0045/h) | ~$1.11 | ~$3.32 |
| Volume 1 GB (SQLite) | $0.15 | $0.15 |
| Root filesystem while stopped | ~$0.10 | – |
| Shared IPv4/IPv6 + certificate | $0 | $0 |
| **Total** | **≈ $1.35–1.40** | **≈ $3.50** |

### fly.toml

```toml
app = "epiaku-freellmapi"
primary_region = "ams"

[build]
  image = "ghcr.io/tashfeenahmed/freellmapi:v0.12.0"   # pin a release tag instead of :latest

[env]
  NODE_ENV = "production"
  PORT = "3001"
  FREEAPI_BLOCK_PRIVATE_PROVIDER_URLS = "true"
  REQUEST_ANALYTICS_RETENTION_DAYS = "30"

[mounts]
  source = "freellmapi_data"
  destination = "/app/server/data"

[http_service]
  internal_port = 3001
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 0        # 1 = always on, no cold start

  [[http_service.checks]]
    grace_period = "20s"
    interval = "30s"
    method = "GET"
    path = "/api/ping"
    timeout = "5s"

[[vm]]
  size = "shared-cpu-1x"
  memory = "512mb"
```

### Setup commands

```bash
fly apps create epiaku-freellmapi
fly volumes create freellmapi_data --region ams --size 1 -a epiaku-freellmapi
fly secrets set ENCRYPTION_KEY=<value from Proxmox .env> -a epiaku-freellmapi
fly deploy --ha=false
fly scale count 1 -a epiaku-freellmapi
fly certs add llm.epiaku.com -a epiaku-freellmapi
```

{{% alert title="Exactly one machine" color="warning" %}}
`fly launch` creates two machines by default. With volumes, each gets its own database. Always use `--ha=false` / `fly scale count 1`.
{{% /alert %}}

### Migrating from Proxmox

- **Start fresh:** put the `admin` account and provider `keys` in `FREEAPI_CONFIG_JSON` (`fly secrets set FREEAPI_CONFIG_JSON=...`, v0.12.0+), so there is no setup screen. You get a new unified key.
- **Copy the DB:**
  1. On Proxmox, `docker compose stop` and copy `freellmapi.db`.
  2. Use the **same `ENCRYPTION_KEY`**.
  3. Deploy once with `[experimental] cmd = ["sleep", "inf"]`.
  4. `fly ssh sftp shell` → `put freellmapi.db /app/server/data/freellmapi.db`.
  5. `fly ssh console -C "chown node:node /app/server/data/freellmapi.db"`.
  6. Remove the override and redeploy.

### Deploying (no separate registry needed)

- **Stock image:** `[build] image = "ghcr.io/..."` in `fly.toml`, then `fly deploy`.
- **Custom image:** a `Dockerfile` next to `fly.toml`, built by Fly's remote builder and pushed to `registry.fly.io/<app>`.
- **CI:**

```bash
fly tokens create deploy -a epiaku-freellmapi
```

```yaml
name: deploy-freellmapi
on:
  push:
    branches: [main]
  workflow_dispatch:
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: superfly/flyctl-actions/setup-flyctl@master
      - run: flyctl deploy --remote-only
        env:
          FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN }}
```

- **Deploys have short downtime** (~10–30 s) with one machine. The volume stays attached. Roll back with `fly releases`, and take a volume snapshot before major upgrades: `fly volumes snapshots create <volume-id>`.
- **Security:** the dashboard is public behind its login. With v0.12.0, create the admin account from `FREEAPI_CONFIG_JSON` so the first-run setup window never opens. Optionally put a proxy in front that exposes only `/v1/*`, and use `fly proxy 3001` for admin access.

---

## 🏠 Running on the H4 {#running-h4}

FreeLLMApi runs as a Docker Compose service in its LXC. The idea catcher pipeline can run as a second service in the same Compose file, or in its own LXC (see the [pipeline page](../../gemini/idea-catcher-pipeline/#implementation)).

```yaml
# docker-compose.yml
services:
  freellmapi:
    image: ghcr.io/tashfeenahmed/freellmapi:v0.12.0    # pin a release; multi-arch
    env_file: .env                                     # ENCRYPTION_KEY etc.
    ports: ["0.0.0.0:3001:3001"]                       # LAN only; never port-forward on UniFi
    volumes: ["freellmapi-data:/app/server/data"]
    restart: unless-stopped
volumes:
  freellmapi-data:
```

Clients (the pipeline, laptops, dev tools) call `http://<h4-ip>:3001/v1` with the unified API key.

---

## 🖥️ Home Hardware (H4, N2, Windows/Mac) {#hardware}

### ODROID H4 vs ODROID N2

| | N2 (separate box) | Extra LXC on the H4 |
|---|---|---|
| Architecture | arm64 (S922X, 2/4 GB RAM) | x86 |
| Setup | Armbian + Docker; move FreeLLMApi | Add one LXC |
| Isolation | Separate hardware / VLAN | Shared host |
| Maintenance | One more device | Covered by Proxmox backups |

- **Decision: use the H4** (least work, one FreeLLMApi, backups included).
- If the N2 is used: Armbian on **eMMC or a USB SSD, not microSD** (SQLite wear); Docker Compose instead of Proxmox (Proxmox is x86-only, ARM ports are unofficial); fixed IP; nightly `sqlite3 .backup`; **move** FreeLLMApi instead of running a second copy.

### Proxmox on Windows / Mac

Proxmox VE is a **bare-metal OS**, not an app.

| Machine | Proxmox? | Alternative |
|---|---|---|
| Windows PC (x86) | ✅ bare metal (wipes Windows); nested VM for testing only | Docker Desktop (WSL2) / Hyper-V |
| Intel Mac | ✅ bare metal (Wi-Fi / T2 quirks) | Docker Desktop / OrbStack |
| Apple Silicon Mac | ❌ official; unofficial ARM ports in UTM | OrbStack / Colima (arm64 images) |

Desktops and laptops are **clients** (`http://<h4-ip>:3001/v1`) and local test environments (`docker compose up`), not servers.

---

## 🚀 Next Steps {#next-steps}

1. **Upgrade the H4 instance to v0.12.0.** Back up the DB first: one migration backfills request history.
2. **Consider an upstream issue/PR** for a `FREEAPI_UNIFIED_API_KEY` env override, the last missing piece for a fully stateless container.
3. **Later, if a web app needs a shared cloud LLM API:** FreeLLMApi on Fly.io with a volume, FreeLLMApi on ACA with Litestream → Azure Blob, or a paid API key.
4. **Before choosing ACA + Litestream:** run the local Docker Compose test (scenarios 1–4) to confirm restore works and whether Litestream flushes on shutdown, then pick the sync interval.
