# Idea Catcher Stage C: the API, Locally Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A small FastAPI service (`catcher api`, container `api`) that starts runs, publishes and requeues by enqueueing jobs, and reads jobs, items, the YouTube gate and the worker's health from Postgres, behind scoped API keys.

**Architecture:** The API never does long work and holds no Git, no LLM key and no repo checkout: it only reads Postgres and inserts jobs through the existing queue module. The worker does everything else. Auth is a Bearer key from `.env` with the scopes `read` and `run`. `/health` reports the database and whether a worker holds the one-worker lock.

**Tech Stack:** Python 3.12, FastAPI + uvicorn (new), SQLAlchemy 2 sync sessions (sync `def` endpoints, run by FastAPI in its thread pool), Typer, pytest + testcontainers, httpx (dev, for FastAPI's `TestClient`).

**Spec:** `docs/idea-catcher-service-architecture.md`: "API" (about line 414: endpoints, scopes, `202`/`200` rules), "Security" (about line 429: Bearer key on every endpoint except `/health`, keys in `.env` as `name:scopes:key`, scopes `read` and `run`, LAN only), Stage C table (about line 1042: C1-C4, "Done when"), the compose sketch (about line 463: `catcher api --host 0.0.0.0 --port 8000`, "No Git, no LLM calls, no long work"), and the Stage C notes (about lines 771-773: the image must copy `alembic.ini` and `migrations/`; a deployed Stage C must fail loudly when `DATABASE_URL` is not set).

## Global Constraints

- Postgres is the single truth; the API needs the database: if it is down, `/health` is `503` and every other endpoint `503`, nothing is cached.
- Every endpoint except `/health` needs `Authorization: Bearer <key>`; a missing, malformed or unknown key is `401`; a key without the needed scope is `403`. Keys are compared in constant time and never logged, never echoed in an error, never in the OpenAPI schema.
- Scopes: `read` (GET endpoints), `run` (POST endpoints). `run` does not imply `read`.
- The API container gets only what it needs (`DATABASE_URL`, `API_KEYS`, `LOG_LEVEL`): no `env_file`, no `GITHUB_TOKEN`, no LLM keys, no repos volume.
- All endpoints live under `/api/v1` except `/health`; Swagger UI at `/docs`.
- No live LLM or YouTube call anywhere; the network guard stays on; db tests use testcontainers, never the user's `catcher-db`; never read `.env`; never touch `tests/data`, `tmp/ic`, `~/.catcher`.
- Agents commit locally, never push (the user pushes).

## Decisions made in this plan (defaults; the user can change them)

1. **Keys:** one setting `API_KEYS`, entries separated by whitespace, each `name:scope[,scope]:key` (for example `mac:read,run:abc… phone:read:def…`). Names are `[a-z0-9_-]{1,32}`, a key is at least 24 characters, scopes are `read` and `run`. No keys configured, a malformed entry, a short key or a duplicate name or key makes `catcher api` exit 2 before it listens, naming the problem (never the key).
2. **`catcher api` refuses the built-in development `DATABASE_URL`:** it exits 2 unless `DATABASE_URL` is set in the environment (a `.env` loaded at startup counts). This closes the Stage C note about failing loudly when `DATABASE_URL` is not set.
3. **`/health`:** `200 {"api": "ok", "database": "ok", "worker": "running"}` when the database answers and a worker holds the lock (`worker_running`, the check `catcher health` uses); `503` with the same shape and the failing part named (`"database": "down"`, `"worker": "none"`) otherwise. No key needed, no secrets in the body.
4. **`/docs` and `/openapi.json` are public** (they hold no data; Swagger UI is the MVP's interface, with an Authorize button for the Bearer key); `API_DOCS=false` turns them off.
5. **`POST /api/v1/pipeline/runs`** body `{ "dry_run": false, "profile": null, "limit": null, "retry_deferred": false }` (all optional). A normal run enqueues `pipeline.run`; `dry_run: true` enqueues a new read-only job type **`pipeline.preview`** that the worker runs (it holds the repos): its result is the preview report. Params go through `check_job`; a refusal is `422` with the handler's message. Dedupe: if any `pipeline.run` (scheduled or API) is `queued` or `running`, return `200` with that job (`"existing": true`); else insert and return `202`. Two simultaneous requests cannot both insert (a partial-unique `dedupe_key` `api:pipeline.run`).
6. **Other triggers (`run` scope):** `POST /api/v1/pipeline/publish` (enqueues `pipeline.publish` `{"pull": true, "push": true}` with the same queued/running dedupe), `POST /api/v1/items/{name}/requeue` (enqueues `pipeline.run` `{"requeue": [name]}`; `404` for an unknown item; the same dedupe rule does NOT apply: a requeue is its own job).
7. **Reads (`read` scope):** `GET /api/v1/jobs?type=&status=&from=&to=&limit=&offset=`, `GET /api/v1/jobs/{id}` (status, params, result, error, timings, events, and for `pipeline.run` the item counts per class and status), `GET /api/v1/items?doc_class=&status=&from=&limit=&offset=` (`status=stuck` works), `GET /api/v1/youtube/gate` (the state `catcher youtube gate` shows). Lists are `{"items": [...], "total": n, "limit": n, "offset": n}`, newest first, `limit` default 50, max 200.
8. **The API container is internal-first:** published as `${API_BIND:-127.0.0.1}:${API_PORT:-8000}:8000`; the LAN needs `API_BIND=0.0.0.0` on purpose.
9. **Out of scope** (spec says future): metrics aggregation, replay, cancel, profiles endpoints, keys in the database, live progress stream, rate limiting, HTTPS (LAN/VPN only).

## Review Focus

- A request with no key, a wrong key, a key with a prefix of a valid key, a key with the wrong scope, `Bearer` with extra spaces, an empty `Authorization` header: `401`/`403`, never a `500`, never a hint which part was wrong beyond the status; a timing-safe compare across all configured keys.
- The key or any part of `API_KEYS` appearing in logs, tracebacks, error bodies, `/openapi.json` or the startup message.
- Two `POST /pipeline/runs` at the same moment, or one while the scheduler's `pipeline.run` is queued: one job, the second answer `200` with the same `job_id`.
- A database that goes away mid-request or at startup: `503`/clear exit, no stack trace in the body, the pool recovers when it returns.
- Huge or hostile query values (`limit=-1`, `limit=1000000`, `offset=-5`, `from=not-a-date`, `status=bogus`, a 10 000-character `type`): `422` or a clamped value, never a 500 or an unbounded query.
- The API container holds no Git token, no LLM key and no repo: `docker compose exec api env` shows only the three variables.

## File structure

- Create `src/catcher/api/__init__.py`, `app.py` (factory, lifespan, error handlers), `auth.py` (key parsing + dependency), `deps.py` (engine/session dependency), `schemas.py` (pydantic models), `routes_health.py`, `routes_jobs.py`, `routes_runs.py`, `routes_items.py`, `routes_youtube.py`.
- Modify `src/catcher/core/config.py` (`api_keys`, `api_docs`), `src/catcher/cli.py` (`catcher api`), `src/catcher/modules/worker/handlers_pipeline.py` and `app.py` (`pipeline.preview`), `pyproject.toml` / `uv.lock` (`fastapi`, `uvicorn`, dev `httpx`), `compose.yaml`, `scripts/compose-smoke`, `.env.example`, docs.
- Tests: `tests/unit/test_api_keys.py`, `tests/integration/db/test_api_auth.py`, `test_api_health.py`, `test_api_jobs.py`, `test_api_runs.py`, `test_api_items.py`, `test_api_gate.py`, `test_handler_preview.py`, `tests/unit/test_deploy_files.py` (extend).

---

### Task 1: App skeleton, API keys, `/health`, `catcher api` (strict review: authentication)

**Files:**
- Create: `src/catcher/api/{__init__,app,auth,deps,routes_health}.py`
- Modify: `src/catcher/core/config.py`, `src/catcher/cli.py`, `pyproject.toml` / `uv.lock` (`uv add fastapi uvicorn`, `uv add --dev httpx`)
- Test: `tests/unit/test_api_keys.py`, `tests/integration/db/test_api_auth.py`, `tests/integration/db/test_api_health.py`

**Interfaces:**
- Produces `Settings.api_keys: SecretStr = SecretStr("")`, `Settings.api_docs: bool = True`.
- Produces `auth.py`: `@dataclass(frozen=True) class ApiKey: name: str; scopes: frozenset[str]; secret: str`; `parse_api_keys(raw: str) -> list[ApiKey]` (raises `ValueError` naming the problem, never the key: decision 1); `def require(scope: str) -> Callable[..., ApiKey]` a FastAPI dependency factory (`HTTPBearer(auto_error=False)`; `401` with `WWW-Authenticate: Bearer` for a missing/unknown key; `403` for a missing scope; compares with `hmac.compare_digest` against every configured key without early exit).
- Produces `app.py`: `create_app(settings: Settings | None = None) -> FastAPI` (lifespan creates one engine with `make_worker_engine`-style lock timeout for the database URL and disposes it; stores `ApiKey`s on `app.state`; `docs_url`/`openapi_url` follow `api_docs`; OpenAPI declares the Bearer scheme; a generic handler turns an unexpected exception into `500 {"detail": "internal error"}` and logs the exception class only, an `OperationalError` into `503 {"detail": "database unavailable"}`).
- Produces `deps.py`: `get_session(request) -> Iterator[Session]` (one session per request, closed after).
- Produces `GET /health` as decision 3.
- Produces CLI `catcher api [--host 127.0.0.1] [--port 8000]`: validates keys and the `DATABASE_URL` rule (exit 2, decision 2), then `uvicorn.run(create_app(), host=..., port=..., log_config=None)` so the project's logging stays in charge; no key in any message.

- [ ] **Step 1: Write the failing tests:** unit `test_parse_api_keys_accepts_the_documented_format`, `..._rejects_empty_short_duplicate_and_malformed_entries_without_echoing_the_key`; integration (TestClient on a testcontainers database) `test_no_key_is_401_with_www_authenticate`, `test_a_wrong_key_and_a_prefix_of_a_valid_key_are_401`, `test_a_key_without_the_scope_is_403`, `test_bearer_with_odd_spacing_or_an_empty_header_is_401_not_500`, `test_the_health_endpoint_needs_no_key_and_reports_database_and_worker` (worker via a held `WorkerLock`), `test_health_is_503_naming_the_failing_part_when_no_worker_holds_the_lock`, `test_health_is_503_when_the_database_is_down` (engine pointed at a dead port), `test_a_database_error_is_503_without_a_traceback_and_an_unexpected_error_is_500_without_details`, `test_the_key_never_appears_in_openapi_logs_or_error_bodies` (caplog), `test_docs_are_served_and_can_be_turned_off`, CLI `test_catcher_api_exits_2_without_keys_or_with_the_default_database_url` (message names the setting, not the key).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_api_keys.py tests/integration/db/test_api_auth.py tests/integration/db/test_api_health.py -x -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the above. Endpoints are sync `def`; no async DB.
- [ ] **Step 4: Run** the tests. Expected: PASS. Then `scripts/check` once.
- [ ] **Step 5: Commit** `feat: the API skeleton with scoped keys, /health and catcher api`.

### Task 2: Read endpoints: jobs, job detail, items, YouTube gate

**Files:**
- Create: `src/catcher/api/{schemas,routes_jobs,routes_items,routes_youtube}.py`
- Modify: `src/catcher/api/app.py` (include routers)
- Test: `tests/integration/db/test_api_jobs.py`, `test_api_items.py`, `test_api_gate.py`

**Interfaces:**
- Consumes Task 1 (`require("read")`, `get_session`), the models `Job`, `JobItem`, `JobEvent` (`catcher.modules.queue.models`), the gate state reader `catcher youtube gate` uses (`PostgresGate` read-only; see `youtube_gate` in `cli.py`).
- Produces the endpoints of decision 7 with pydantic response models: `JobOut` (id, type, status, priority, params, result, error, reason, attempts, run_after, created_at, started_at/finished_at if the model has them), `JobDetailOut(JobOut)` adding `events: list[EventOut]` (oldest first, capped at 200) and `item_counts: dict[str, dict[str, int]]` (class -> status -> n; only for `pipeline.run`, from `job_items.root_job_id`), `ItemOut` (name, doc_id, doc_class, status, stage_reason, stage_since, profile, backend, model, tokens, origin, updated_at, output path if stored), `Page[T]`, `GateOut`.
- Filters validated by pydantic/Query constraints: `status` in the known statuses, `type` max length 64, `from`/`to` ISO datetimes, `limit` 1-200, `offset` >= 0.

- [ ] **Step 1: Write the failing tests:** `test_jobs_list_is_newest_first_paged_and_filtered_by_type_status_and_dates`, `test_jobs_list_rejects_bad_query_values_with_422` (the Review Focus values), `test_job_detail_has_events_and_item_counts_for_a_run`, `test_job_detail_404_for_an_unknown_or_malformed_id`, `test_items_filter_by_class_status_and_stuck_and_page`, `test_items_never_show_file_contents_or_secrets` (only the fields listed), `test_gate_state_matches_catcher_youtube_gate_and_needs_read`, `test_every_read_endpoint_needs_the_read_scope` (parametrised 401/403/200), `test_the_answers_equal_direct_sql_counts` (the Stage C "compare with SQL" check).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** with plain SQLAlchemy `select` + `func.count` for `total`; no N+1 (events and counts in one query each).
- [ ] **Step 4: Run** the tests. Expected: PASS. Then `scripts/check` once.
- [ ] **Step 5: Commit** `feat: the API reads jobs, items and the YouTube gate`.

### Task 3: Start a run, `pipeline.preview`, publish and requeue (strict review: queue)

**Files:**
- Create: `src/catcher/api/routes_runs.py`
- Modify: `src/catcher/modules/worker/handlers_pipeline.py` and `app.py` (`handle_pipeline_preview`, registry, `PARAM_CHECKS`), `src/catcher/api/app.py`
- Test: `tests/integration/db/test_api_runs.py`, `tests/integration/db/test_handler_preview.py`

**Interfaces:**
- Consumes Task 1 (`require("run")`), `queue.enqueue(session, type=, now=, params=, dedupe_key=) -> (job, created)`, `check_job(job_type, params)` from `worker/app.py`, `catcher.modules.pipeline.preview.preview(ideas, docs, params, services) -> RunReport`.
- Produces job type `pipeline.preview` (params = the `pipeline.run` params, validated by `parse_params`): the worker runs `preview(...)` with its context's services and returns `Done({"report": <names and counts as a bounded dict>})`; it changes no file and writes no item row.
- Produces `POST /api/v1/pipeline/runs`, `POST /api/v1/pipeline/publish`, `POST /api/v1/items/{name}/requeue` per decisions 5 and 6; responses `{"job_id": "...", "existing": bool}` with `202` (new) or `200` (existing).

- [ ] **Step 1: Write the failing tests:** `test_post_runs_enqueues_pipeline_run_with_202`, `test_a_second_post_while_one_is_queued_or_running_is_200_with_the_same_job_id`, `test_a_scheduled_pipeline_run_counts_as_existing`, `test_two_simultaneous_posts_make_one_job` (two threads, barrier), `test_dry_run_enqueues_pipeline_preview_not_a_run`, `test_bad_params_are_422_with_the_handler_message` (`limit=-1`, unknown profile name shape), `test_publish_trigger_enqueues_publish_with_dedupe`, `test_requeue_enqueues_a_run_with_only_that_name_and_404s_an_unknown_item`, `test_run_scope_is_required_and_read_does_not_imply_it`, `test_post_does_not_wait_for_the_worker` (returns before any job runs); handler `test_preview_job_returns_the_report_and_changes_nothing` (byte hash of both repos, no rows, no gate change, raising backend/fetcher), `test_preview_job_refuses_bad_params`, `test_the_worker_runs_a_preview_job_end_to_end`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** The existing-job lookup is `select(Job).where(Job.type == type, Job.status.in_(("queued","running"))).order_by(created_at)`; the insert uses the `api:<type>` dedupe key so a lost race falls back to the lookup.
- [ ] **Step 4: Run** the tests. Expected: PASS. Then `scripts/check` once.
- [ ] **Step 5: Commit** `feat: the API starts runs, previews, publishes and requeues by enqueueing jobs`.

### Task 4: The `api` container and the stack smoke run (strict review)

**Files:**
- Modify: `compose.yaml`, `compose.test.yaml`, `scripts/compose-smoke`, `.env.example`, `Dockerfile` only if needed (it already has `curl`), `tests/unit/test_deploy_files.py`
- Test: `tests/unit/test_deploy_files.py` (the compose part)

**Interfaces:**
- Produces in `compose.yaml` a service `api`: `image: idea-catcher:latest` (same build as the others), `entrypoint: []` (no repo cloning: it has none), `command: ["catcher", "api", "--host", "0.0.0.0", "--port", "8000"]`, **no `env_file`**, `environment` = `DATABASE_URL` (same as the worker), `API_KEYS: ${API_KEYS:-}`, `LOG_LEVEL`, `restart: unless-stopped`, `ports: ["${API_BIND:-127.0.0.1}:${API_PORT:-8000}:8000"]`, `depends_on: migrate: {condition: service_completed_successfully}`, `healthcheck: curl -fsS http://127.0.0.1:8000/health` (a `503` while no worker holds the lock makes the api unhealthy: acceptable and documented, or use a lighter check; decide in the task: prefer a check that does not depend on the worker: `curl -fsS http://127.0.0.1:8000/docs -o /dev/null`), interval 30 s.
- Produces smoke steps in `scripts/compose-smoke` (with a dummy key `smoke-api-key-not-real-0000`): the api container becomes healthy; `/health` is 200 once the worker runs; a call without a key is 401; `GET /api/v1/jobs` with the key lists the scheduled jobs the worker queued; `POST /api/v1/pipeline/runs` twice gives `202` then `200` with the same id (stop the scheduler noise: use a schedule-free override for this step or accept `200` first if a scheduled run is queued: assert the second equals the first); the API environment (`docker compose exec -T api env`) holds no `GITHUB_TOKEN`, no `OPENAI_API_KEY` and no repo variables; stopping the worker makes `/health` 503 naming the worker; the key appears in no container log. The smoke teardown and safety rules are the existing ones (own project name, random ports, never `catcher-db`).

- [ ] **Step 1: Write the failing tests:** `test_compose_has_an_api_service_without_env_file_repos_or_secrets`, `test_the_api_waits_for_the_migrations_and_binds_to_localhost_by_default`, `test_env_example_documents_api_keys_api_port_and_api_bind`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_deploy_files.py -x -q`. Expected: FAIL.
- [ ] **Step 3: Edit** compose files and the smoke script; add `--selftest` cases for any new pure check function.
- [ ] **Step 4: Run the smoke for real** (`bash scripts/compose-smoke`; same safety rules as before: unique project, random ports, exact-name teardown, `catcher-db` untouched, no live calls) and `scripts/check` once. Expected: every step PASS.
- [ ] **Step 5: Commit** `feat: the api container joins the stack and the smoke run covers it`.

### Task 5: Docs (single review)

**Files:**
- Modify: `docs/idea-catcher-how-to-run-stage-b.md` or a new `docs/idea-catcher-how-to-run-api.md` (decide: a new page `idea-catcher-how-to-run-api` if the how-to is already long; link it from both how-to pages and the README): setting up `API_KEYS`, `curl` examples for every endpoint (the two from the architecture doc plus the new ones), Swagger UI and the Authorize button, `/health` meaning (database and worker lock; not the scheduler), the 401/403/422/503 table, scopes, `API_BIND` for the LAN, why the API container has no tokens, what is NOT built (decision 9), `docs/idea-catcher-service-architecture.md` (Stage C built with the date; the decisions; the table of endpoints as built; "Open items after Stage C"; remove the two Stage C notes that are now settled), `README.md`, `.env.example` check.

- [ ] **Step 1: Write** the docs from the smoke run and the tests; claim only what was run.
- [ ] **Step 2: Run** `scripts/check` once.
- [ ] **Step 3: Commit** `docs: Stage C built`.

## Self-review

- **Spec coverage:** C1 (Task 1: app, `/health` with the worker check, keys and scopes, `/docs`, a request without a key fails), C2 (Task 3 start a run with dedupe, Task 2 job detail), C3 (Task 2 lists and the gate, Task 3 publish and requeue), C4 (Task 4). The spec's `{ dry_run?, llm? }` is `dry_run` plus `profile` (the existing run parameter for the LLM profile); `llm` as a name would suggest a free-form backend and the spec says there is no per-request backend override.
- **Types and names:** `ApiKey`, `parse_api_keys`, `require`, `create_app`, `get_session`, `JobOut`, `JobDetailOut`, `ItemOut`, `Page`, `GateOut`, `pipeline.preview`, `API_KEYS`, `API_BIND`, `API_PORT` are used identically across tasks.
- **Review Focus:** auth edge cases (Task 1), key leakage (Task 1, Task 4), simultaneous POST (Task 3), database away (Task 1), hostile query values (Task 2), container least privilege (Task 4).
- **Open questions for the user:** the nine decisions at the top. The ones most likely to change: `dry_run` through a new `pipeline.preview` job (versus leaving it to the CLI), public `/docs`, and the single `API_KEYS` format.
