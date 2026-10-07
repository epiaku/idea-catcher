# Idea Catcher Stage B7: the Worker in Compose Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `docker compose up` starts the database, runs the migrations once, and runs `catcher worker` (with its schedules) in a container that keeps its two git clones in a volume, so scheduled runs happen without a terminal.

**Architecture:** One image (Python 3.12, `uv`, Git, `yt-dlp` + Deno) used by a one-shot `migrate` service and the long-running `worker` service. An entrypoint script clones the two repos into the `repos` volume on first start, then `exec`s the command, so `catcher worker` is PID 1 and gets SIGTERM from `docker stop`. Git authenticates with a GitHub token through `GIT_ASKPASS` (the token is never in a remote URL or in `.git/config`).

**Tech Stack:** Docker / Compose v2, `python:3.12-slim` base, `uv`, Deno, bash, pytest (file-level tests: no Docker needed), a smoke script that does the real run.

**Spec:** `docs/idea-catcher-service-architecture.md`: row B7 of the Stage B table (about line 716), "Hosting & Deploy" (about line 437: the compose sketch, "The image holds Python 3.12 + uv, Git, `yt-dlp` + Deno. There is no Node, no Claude Code CLI and no Hugo", `.env` differs per machine, secrets only in `.env`), the Secrets row (about line 433: a fine-grained GitHub PAT, Contents read/write on `idea-bucket` and `epiaku-docs` only).

## Global Constraints

- Postgres is the single truth; the worker container needs the database: if it is down the worker does not run (unchanged).
- No Node, no Claude Code CLI, no Hugo in the image.
- Secrets only in `.env` (git-ignored), never in the image, never in `compose.yaml`, never in a git remote URL. `.env` is never copied into the image (`.dockerignore`).
- The same `compose.yaml` runs on the Mac and later on the LXC; only `.env` differs.
- One worker at a time (the advisory lock, unchanged); no `api` service yet (Stage C), no `deploy.sh` (the Proxmox stage).
- Agents never touch the user's `catcher-db` container or its volume (port 5432); every real run uses its own compose project name, its own `DB_PORT` and a throwaway volume, and removes them afterwards.
- No live LLM or YouTube call in any test or smoke run: `OPENAI_API_KEY=""`, `FREELLMAPI_URL=http://127.0.0.1:1/v1`, `YOUTUBE_OFFLINE=1`; never set `CATCHER_ALLOW_NETWORK`. Never read or print `.env`. Never touch `tests/data`, `tmp/ic`, `~/.catcher`.
- Agents commit locally, never push (the user pushes).

## Decisions made in this plan (defaults; the user can change them)

1. The image runs as a non-root user `catcher` (uid 1000); `git config --system safe.directory '*'` so a bind-mounted checkout works.
2. Git auth is a **GitHub token over https** (spec): `GITHUB_TOKEN` in `.env`, read by `/usr/local/bin/git-askpass` (set as `GIT_ASKPASS`); remotes are plain https URLs. An ssh deploy key mount is not built (can be added later: the code already leaves a user-chosen `GIT_SSH_COMMAND` alone).
3. The entrypoint **clones on first start**: `IDEAS_REMOTE` and `DOCS_REMOTE` (a URL or a path) into `/data/repos/idea-bucket` and `/data/repos/epiaku-docs`; an existing clone is left alone; a missing clone with no remote set is a clear error (exit 2). The container sets `IDEAS_REPO` and `DOCS_REPO` to those paths.
4. Migrations run in a one-shot **`migrate`** service (`catcher db upgrade`); `worker` has `depends_on: migrate: condition: service_completed_successfully`.
5. The published db port is `${DB_PORT:-5432}:5432` (the worker talks to `db:5432` inside the network), so a second stack never collides with the user's `catcher-db`. The password is `${DB_PASSWORD:-catcher}`.
6. **A worker healthcheck now** (user decision 2026-10-07): a new read-only command `catcher health` (Task 3) tells whether a worker holds the one-worker advisory lock in Postgres; Compose runs it as the `worker` healthcheck. `restart: unless-stopped`, `init: false` (the entrypoint `exec`s), `stop_grace_period: 120s` (a running job finishes first; after the grace Docker kills it and the reaper requeues it).
7. While the worker container runs, `catcher run pipeline` and `catcher publish` refuse (the one-worker lock): to run by hand, `docker compose stop worker` first. Documented, not changed.
8. Logs go to stdout (`docker compose logs -f worker`); `LOG_FILE` stays unset.

## Review Focus

- A token in the wrong place: `GITHUB_TOKEN` must not appear in `.git/config`, in `docker compose config` of a committed file, in `docker history`, nor in the worker log (git's error messages can echo a remote URL: remotes must carry no credentials).
- First start with no network or a wrong remote: the entrypoint fails with a clear message and the container does not loop silently (restart policy backs off; the message names `IDEAS_REMOTE`).
- A half-cloned repo from an interrupted first start: the next start must not treat a directory without `.git` as a clone, nor `rm -rf` anything it did not create.
- `docker compose stop` while a job runs: SIGTERM reaches `catcher worker` (PID 1 via `exec`), the worker finishes the job and exits 0; data in the `repos` volume and the database survive `docker compose down` (not `-v`).
- The `.env` that works on the host (relative `IDEAS_REPO=../idea-bucket`, `DATABASE_URL` with `localhost`) must not leak into the container: the compose `environment:` overrides win.
- The build context: `.env`, `.git`, `.venv`, `tmp/`, `tests/data` and the caches never enter the image.

## File structure

- Create `Dockerfile`, `.dockerignore`, `scripts/docker-entrypoint.sh`, `scripts/git-askpass.sh`, `compose.test.yaml` (smoke overrides), `scripts/compose-smoke`.
- Create `tests/unit/test_deploy_files.py` (Dockerfile, compose, `.dockerignore` invariants), `tests/unit/test_docker_entrypoint.py` (the entrypoint and askpass scripts, run in a temp dir with real git and local bare repos).
- Modify `compose.yaml` (`migrate`, `worker`, `DB_PORT`, `DB_PASSWORD`), `.env.example` (the new variables), docs, README.

---

### Task 1: The entrypoint and the git askpass scripts (strict review: secrets, git)

**Files:**
- Create: `scripts/docker-entrypoint.sh`, `scripts/git-askpass.sh`
- Test: `tests/unit/test_docker_entrypoint.py`

**Interfaces:**
- Produces `scripts/docker-entrypoint.sh` (bash, `set -euo pipefail`): reads `IDEAS_REPO`, `DOCS_REPO`, `IDEAS_REMOTE`, `DOCS_REMOTE`; for each repo: when `<repo>/.git` exists, do nothing; when the directory is missing or empty and a remote is set, `git clone "$remote" "$repo"`; when the directory exists, is non-empty and has no `.git`, or when nothing exists and no remote is set, print `entrypoint: <repo> is not a git checkout and <NAME>_REMOTE is not set` (naming the variable) to stderr and exit 2, touching nothing; finally `exec "$@"`. A failed clone removes only the empty directory it created (never a non-empty one) and exits with git's code after printing which remote failed (the URL printed with any `user:pass@` part replaced by `***@`).
- Produces `scripts/git-askpass.sh` (sh): git calls it with the prompt as `$1`; answers `x-access-token` for a prompt starting with `Username` and `$GITHUB_TOKEN` for `Password`; with `GITHUB_TOKEN` empty it prints nothing and exits 1. It never echoes the token to stderr.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_docker_entrypoint.py`, bash via `subprocess`, local bare repos, no network): `test_a_missing_repo_is_cloned_from_its_remote`, `test_an_existing_clone_is_left_alone` (a local commit survives; no fetch needed), `test_a_directory_without_git_is_never_touched` (non-empty dir with a file: exit 2, file still there, message names the variable), `test_a_missing_repo_without_a_remote_exits_2_naming_the_variable`, `test_a_failed_clone_removes_only_the_empty_directory_it_made_and_hides_credentials` (remote `https://user:secret@127.0.0.1:1/x.git`: exit non-zero, `secret` not in stdout/stderr, a pre-existing non-empty sibling untouched), `test_the_command_is_exec_ed_with_its_arguments` (the entrypoint runs `sh -c 'echo $$'` and compares with its own pid via `exec`, or checks argument passing and exit code), `test_askpass_answers_username_and_token_and_never_prints_the_token_to_stderr`, `test_askpass_without_a_token_fails`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_docker_entrypoint.py -x -q`. Expected: FAIL (scripts missing).
- [ ] **Step 3: Implement** both scripts (executable bit set and committed as such: `git update-index --chmod=+x` if needed; a test checks the mode).
- [ ] **Step 4: Run** the tests. Expected: PASS. Then `scripts/check` once.
- [ ] **Step 5: Commit** `feat: the container entrypoint clones the two repos on first start and git asks for the token through askpass`.

### Task 2: The Dockerfile and `.dockerignore`

**Files:**
- Create: `Dockerfile`, `.dockerignore`
- Test: `tests/unit/test_deploy_files.py` (the Dockerfile and `.dockerignore` part)

**Interfaces:**
- Produces `Dockerfile`: base `python:3.12-slim-bookworm`; apt: `git`, `openssh-client`, `ca-certificates`, `curl`, `unzip`; Deno installed from the official release zip with the version in `ARG DENO_VERSION` (pin the current stable at implementation time and name it in the report); `uv` copied from `ghcr.io/astral-sh/uv` (pin a version in an `ARG`); app in `/app`, dependencies installed with `uv sync --frozen --no-dev` (layer cached before copying `src/`), the package installed so `catcher` is on `PATH` and `PROJECT_ROOT` (parents of `core/config.py`) resolves to `/app` where `profiles.yaml` and `migrations/` + `alembic.ini` are copied; non-root user `catcher` (uid 1000) owning `/app` and `/data/repos`; `git config --system safe.directory '*'`; `ENV GIT_ASKPASS=/usr/local/bin/git-askpass`; scripts copied to `/usr/local/bin/docker-entrypoint` and `/usr/local/bin/git-askpass`; `ENTRYPOINT ["docker-entrypoint"]`, `CMD ["catcher", "worker"]`; `HEALTHCHECK NONE` is not written (no healthcheck in B7). No Node, no Claude Code CLI, no Hugo.
- Produces `.dockerignore`: `.env`, `.env.*`, `.git`, `.venv`, `tmp`, `tests`, `docs`, `research_notes`, `reports`, `.superpowers`, caches, `.claude`, `.cline`, `.clinerules`, `.roo`, `*.log`. Keep `!.env.example` out of the image too (not needed).

- [ ] **Step 1: Write the failing tests** (parse the files as text): `test_the_dockerfile_installs_git_and_deno_and_pins_both_tool_versions`, `test_the_dockerfile_runs_as_a_non_root_user`, `test_the_dockerfile_never_copies_env_or_sets_a_secret` (no `COPY .env`, no `ENV`/`ARG` with TOKEN/PASSWORD/KEY values), `test_the_dockerfile_has_no_node_claude_or_hugo`, `test_the_image_entrypoint_and_default_command`, `test_dockerignore_keeps_secrets_and_state_out_of_the_build_context` (all the entries above), `test_the_scripts_are_executable_files_in_git`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_deploy_files.py -x -q`. Expected: FAIL.
- [ ] **Step 3: Write** `Dockerfile` and `.dockerignore`. Build it for real once: `docker build -t idea-catcher:b7-test .` and check, inside a throwaway `docker run --rm`: `catcher --help`, `deno --version`, `yt-dlp --version`, `git --version`, `id -u` is 1000, `python -c "from catcher.core.config import PROJECT_ROOT; print(PROJECT_ROOT)"` prints `/app` and `/app/profiles.yaml` exists, `docker history` shows no token. Then remove that image tag. (Building needs network to apt/PyPI/GitHub releases: this is a build, not an app call; allowed.)
- [ ] **Step 4: Run** the unit tests. Expected: PASS. Then `scripts/check` once.
- [ ] **Step 5: Commit** `feat: the Dockerfile for the worker image and a .dockerignore that keeps secrets out`.

### Task 3: `catcher health` (single review)

**Files:**
- Modify: `src/catcher/cli.py`, `src/catcher/modules/worker/guard.py` (a read-only `worker_running(engine, key=DEFAULT_KEY) -> bool`)
- Test: `tests/integration/db/test_health_cli.py`

**Interfaces:**
- Produces `worker_running(engine: Engine, key: int = DEFAULT_KEY) -> bool`: one read-only query on `pg_locks` for a granted session-level advisory lock with that key (same `classid`/`objid`/`objsubid = 1` decoding as `_HELD` in `guard.py`); it never takes the lock.
- Produces `catcher health`: prints `worker running` and exits 0 when a worker holds the lock; prints `no worker holds the lock` and exits 1 when none does; exits 2 with the usual message when `DATABASE_URL` is malformed or the database cannot be reached (the lock needs no tables, so a database that was never upgraded is fine). It writes nothing and takes no lock, so it never makes a starting worker exit 2, and it is cheap enough to run every 30 s.

- [ ] **Step 1: Write the failing tests:** `test_health_is_0_while_a_worker_holds_the_lock` (a `WorkerLock` on a second connection), `test_health_is_1_when_nobody_holds_the_lock`, `test_health_is_1_after_the_worker_lock_is_released`, `test_health_takes_no_lock_and_a_worker_can_still_start_right_after`, `test_health_exits_2_for_an_unreachable_database_and_a_malformed_url`, `test_health_does_not_see_a_different_advisory_key` (another key held: still exit 1).
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_health_cli.py -x -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `worker_running` and the command (reuse `_check_database_url` and the OperationalError mapping of the `worker` command).
- [ ] **Step 4: Run** the tests. Expected: PASS. Then `scripts/check` once.
- [ ] **Step 5: Commit** `feat: catcher health tells whether a worker holds the one-worker lock`.

### Task 4: `compose.yaml` with `migrate` and `worker`, and `.env.example`

**Files:**
- Modify: `compose.yaml`, `.env.example`
- Test: `tests/unit/test_deploy_files.py` (the compose part; parse with `yaml.safe_load`)

**Interfaces:**
- Produces in `compose.yaml` (keep `db` and its volume name; add): `x-app` anchor (`build: .`, `image: idea-catcher:latest`, `env_file: .env`, `environment:` with `DATABASE_URL: postgresql+psycopg://catcher:${DB_PASSWORD:-catcher}@db:5432/catcher`, `IDEAS_REPO: /data/repos/idea-bucket`, `DOCS_REPO: /data/repos/epiaku-docs`, `IDEAS_REMOTE: ${IDEAS_REMOTE:-}`, `DOCS_REMOTE: ${DOCS_REMOTE:-}`, `GITHUB_TOKEN: ${GITHUB_TOKEN:-}`); service `migrate` (`command: ["catcher", "db", "upgrade"]`, `restart: "no"`, `depends_on: db: service_healthy`, no `repos` mount); service `worker` (`command: ["catcher", "worker"]`, `restart: unless-stopped`, `stop_grace_period: 120s`, `healthcheck: {test: ["CMD", "catcher", "health"], interval: 30s, timeout: 10s, retries: 3, start_period: 60s}`, `volumes: [repos:/data/repos]`, `depends_on: migrate: {condition: service_completed_successfully}`); `db` ports `"${DB_PORT:-5432}:5432"` and `POSTGRES_PASSWORD: ${DB_PASSWORD:-catcher}` with a matching `pg_isready` healthcheck; volumes `catcher-pgdata` and `repos`. Header comment updated (how to start the stack, `docker compose stop worker` before running `catcher run pipeline` by hand).
- Produces in `.env.example`: commented `DB_PASSWORD`, `DB_PORT`, `IDEAS_REMOTE`, `DOCS_REMOTE`, `GITHUB_TOKEN` (with the fine-grained token scope from the spec) and a note that inside Compose `DATABASE_URL`, `IDEAS_REPO`, `DOCS_REPO` are set by `compose.yaml` and that `FREELLMAPI_URL` must be reachable from the container (a LAN address, or `host.docker.internal` on the Mac).

- [ ] **Step 1: Write the failing tests:** `test_compose_has_db_migrate_and_worker_and_no_api_yet`, `test_the_worker_waits_for_the_migrations_to_complete`, `test_the_worker_keeps_the_repos_in_a_named_volume_and_restarts_unless_stopped`, `test_the_container_environment_overrides_host_only_paths` (`DATABASE_URL` host is `db`, repo paths under `/data/repos`), `test_no_secret_value_is_written_in_compose_yaml` (every secret is a `${VAR:-}` reference; the only literal password is the dev default `catcher`), `test_the_db_port_is_configurable`, `test_the_worker_gets_a_grace_period_to_finish_its_job`, `test_the_worker_has_a_catcher_health_healthcheck`, `test_env_example_documents_every_variable_compose_reads` (each `${NAME}` in compose.yaml appears in `.env.example`).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Edit** both files. Check `docker compose config` parses (with `DB_PORT=55432` set) without printing secrets.
- [ ] **Step 4: Run** the tests. Expected: PASS. Then `scripts/check` once.
- [ ] **Step 5: Commit** `feat: compose runs the migrations once and the worker with its schedules`.

### Task 5: The real run: smoke script and `compose.test.yaml` (strict review)

**Files:**
- Create: `compose.test.yaml`, `scripts/compose-smoke`

**Interfaces:**
- Produces `compose.test.yaml`: overrides for a smoke run only: mounts `${SMOKE_REMOTES}:/remotes` (read-write: pushes go to bare repos there), `IDEAS_REMOTE: /remotes/idea-bucket.git`, `DOCS_REMOTE: /remotes/epiaku-docs.git`, `OPENAI_API_KEY: ""`, `FREELLMAPI_URL: http://127.0.0.1:1/v1`, `YOUTUBE_OFFLINE: "1"`, `SCHEDULE_IDEAS_PULL`, `SCHEDULE_PIPELINE_RUN`, `SCHEDULE_PUBLISH` all `"* * * * *"`, `SCHEDULE_TIMEZONE: UTC`, `GITHUB_TOKEN: ""`.
- Produces `scripts/compose-smoke` (bash, `set -euo pipefail`): picks a random free host port and a unique project name (`catcher-smoke-$$`), builds throwaway bare repos from a copy of the test fixtures in a temp dir (read how `catcher testdata reset` / the B6 report `.superpowers/sdd/2026-10-07-idea-catcher-stage-b6-scheduler-in-the-worker/task-6-report.md` built local bare remotes; never touch `tests/data` itself), runs `docker compose -p <name> -f compose.yaml -f compose.test.yaml up -d --build`, then asserts and prints PASS/FAIL per step, and ALWAYS tears down (`down -v`, remove the temp dir, remove the image tag) in a `trap`: (a) `migrate` exited 0 and the worker becomes `healthy` (`docker compose ps`), and `unhealthy` is NOT reported while it runs; (b) the repos were cloned into the volume (`docker compose exec -T worker git -C /data/repos/idea-bucket log -1`); (c) within ~150 s `catcher jobs list` shows a job of each type `ideas.pull`, `pipeline.run`, `pipeline.publish` and `catcher schedules` shows all three with `last fired`; (d) a capture pushed into the bare idea-bucket remote from another clone arrives in the container's clone (pulled by the schedule); (e) `docker compose stop worker` ends within the grace period with exit code 0 and the log line `worker ... stopped`; (f) after `docker compose up -d worker` again the repos and jobs are still there (volume and database survived) and the schedule fires once, not a flood; (g) `docker compose exec -T worker env` does not contain a token and `docker history idea-catcher` shows none; (h) `catcher run pipeline` inside the running worker container exits 2 with the one-worker message.
- Never runs against `catcher-db`, never uses port 5432, never calls an LLM or YouTube.

- [ ] **Step 1: Write** `compose.test.yaml` and `scripts/compose-smoke` (make it executable).
- [ ] **Step 2: Run it for real** (`bash scripts/compose-smoke`; long steps in the background with sleeps under 500 s, the Mac may sleep: re-check the clock rules, a missed slot runs once). Expected: every step PASS and the teardown leaves no container, volume, network or image of the smoke project (`docker ps -a`, `docker volume ls`, `docker network ls` filtered by the project name show nothing).
- [ ] **Step 3: Fix** what the real run finds in the files of Tasks 1-3 (small, separate commits `fix: ...` with a test in `tests/unit/` where a unit test can catch it); a bug in `src/` is reported, not fixed silently.
- [ ] **Step 4: Run** `scripts/check` once.
- [ ] **Step 5: Commit** `feat: a compose smoke run that proves the worker pulls, runs, publishes and stops cleanly`.

### Task 6: Docs (single review)

**Files:**
- Modify: `docs/idea-catcher-how-to-run-stage-b.md` (a "Run it in Docker" section: first start with a token and the two remotes, what you see in `docker compose logs -f worker`, `docker compose exec worker catcher schedules` / `jobs list` / `items list`, stopping and updating (`docker compose up -d --build`), running by hand (`docker compose stop worker` first), where the data lives and how to wipe it (`down -v` deletes the DB and the clones), the smoke script), `docs/idea-catcher-how-to-run.md` (a pointer), `docs/idea-catcher-service-architecture.md` (B7 built with the date; the decisions above; open items: the healthcheck only proves a worker holds the lock (not that the scheduler thread ticks), no `deploy.sh`, no ssh key mount, `host.docker.internal` for a Mac-local FreeLLMApi), `README.md` status.

- [ ] **Step 1: Write** the docs from what the smoke run proved and what the tests pin; claim nothing that was not run.
- [ ] **Step 2: Run** `scripts/check` once.
- [ ] **Step 3: Commit** `docs: Stage B7 built`.

## Self-review

- **Spec coverage:** worker next to `db` (Task 3), `repos` volume (Task 3), image with Python 3.12, `uv`, Git, `yt-dlp` + Deno and no Node/Claude/Hugo (Task 2), secrets only in `.env` and a PAT with minimal scope (Tasks 1, 3, 2's `.dockerignore`), "watch scheduled runs happen" (Task 4), same `compose.yaml` on Mac and LXC (Task 3 uses env only). `deploy.sh` and `api` are in the spec's compose sketch but belong to the Proxmox stage and Stage C: named in Task 5's open items.
- **Types and names:** `IDEAS_REMOTE`, `DOCS_REMOTE`, `GITHUB_TOKEN`, `DB_PORT`, `DB_PASSWORD`, `/data/repos/idea-bucket`, `/data/repos/epiaku-docs`, the scripts' install paths, and the service names `db`/`migrate`/`worker` are used identically in Tasks 1-5.
- **Review Focus:** token placement (Tasks 1, 2, 4g), failed first start and half clones (Task 1), stop and persistence (Task 4e, 4f), host `.env` leakage (Task 3), build context (Task 2).
- **Settled by the user (2026-10-07):** https token, a healthcheck now, agents may build and run throwaway Docker images.
