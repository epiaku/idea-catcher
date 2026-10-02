# Idea Catcher Stage B (B0–B2): steppable run, database, queue core

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Stage A run callable one step at a time (B0), add the Postgres schema (B1) and our own job queue with leases and fencing (B2), without changing what `catcher run pipeline` does.

**Architecture:** B0 extracts pure, testable pieces from `run_pipeline` (`classify`, `split_duplicates`, `load_staged_note`, three processing steps, a `Gate` protocol) and leaves `run_pipeline` as a thin orchestrator over them, so every Stage A test stays green and unchanged. B1 and B2 add a new `catcher.modules.queue` package on sync SQLAlchemy 2 + psycopg 3 with Alembic; every function takes `now` from the caller. Tests run against a real Postgres 17 in Docker.

**Tech Stack:** Python 3.12, uv, SQLAlchemy 2, psycopg 3, Alembic, testcontainers (dev), Docker image `pgvector/pgvector:pg17`, pytest, ruff, pyright.

**Spec:** `docs/idea-catcher-service-architecture.md`, section "Stage B: Postgres + the queue", especially **"Stage B decisions (2026-10-02…)"** (decisions 1–14 win over the step texts), plus `docs/idea-catcher-youtube-bans-and-queue-options.md` ("Where the state lives"). Out of scope here: B3–B8 (handlers, gate in Postgres, state/metrics, scheduler, worker container, backfill), the API.

## Global Constraints

- Stage A behaviour is frozen: `uv run pytest tests/unit tests/component tests/integration/git -q` must stay green, **with the existing tests unchanged**, after every B0 task. New tests are added, old ones are not edited (a deliberate behaviour change is a separate task and none is planned).
- Every queue and gate function takes `now: datetime` (timezone-aware) from the caller. **Never SQL `now()`**; a naive datetime raises `ValueError`.
- Job status is one of `queued`, `running`, `succeeded`, `failed`, `cancelled` (text + CHECK constraint, not a Postgres enum). Deferral is `queued` with a future `run_after` and a `reason`. A **higher `priority` number goes first**; the claim order is `priority DESC, run_after, created_at`.
- Sync SQLAlchemy 2 with psycopg 3; Alembic migrations; Postgres 17 via `pgvector/pgvector:pg17`. No mock of the queue, ever.
- The only attempt counter is for crashes: a claim counts one attempt, a lease that expires too often (`max_attempts`, default 3) makes the job `failed`. There is no retry layer for LLM errors (decision 3).
- Line length 110, ruff (`E F I UP B SIM`), pyright standard on `src`. No live LLM or YouTube/yt-dlp call in any test. The only network use is pulling the Postgres image from Docker Hub.
- The pre-commit hook runs only `tests/unit` and `tests/component`. Database tests live in `tests/integration/db` and run with `uv run pytest tests/integration/db -q`; run them before every commit that touches `modules/queue` or `core/db.py`.
- Commit only when the user says so (this plan's "Commit" steps are for the executor once authorised); end commit messages with the attribution line the session requires.

## Review Focus

Failure modes the spec implies that are most likely to bite, each pinned by a test in the named task:

1. **Two workers claim at once:** exactly one gets the job (Task 10).
2. **A worker is reaped but keeps running and then calls `complete`:** the late call is refused and the re-claimed job is untouched (Task 11).
3. **A poison job kills every worker that claims it:** it ends `failed` after `max_attempts`, not an endless loop (Task 12).
4. **Two simultaneous enqueues with the same `dedupe_key`:** one row, both callers get the same job id (Task 9).
5. **A naive datetime or SQL clock sneaks in:** rejected with `ValueError`; all time tests use a frozen clock (Tasks 7, 9).

---

## B0: make the run steppable (no behaviour change)

### Task 1: `classify` turns an exception into an `Outcome`

**Files:**
- Create: `src/catcher/modules/pipeline/outcome.py`
- Test: `tests/component/test_outcome.py`

**Interfaces:**
- Produces: `OutcomeKind = Literal["deferred", "failed", "waiting", "would_fetch", "interrupted"]`; `@dataclass(frozen=True) class Outcome: kind: OutcomeKind; message: str; backend: str | None = None; budget: bool = False; config_error: bool = False`; `def classify(error: BaseException) -> Outcome`.

Mapping (this is today's `except` ladder in `run.py`; order matters because several are subclasses):

| Exception | kind | extra |
| --- | --- | --- |
| `KeyboardInterrupt` | `interrupted` | message `back in inbox/` |
| `BudgetExhausted` | `deferred` | `backend`, `budget=True`, message `budget reached (<backend>)` |
| `UsageLimitReached` | `deferred` | `backend`, message `usage limit (<backend>): <error>` |
| `BackendUnavailable` | `deferred` | message `str(error)` |
| `UnknownProfile` | `deferred` | `config_error=True` |
| `InvalidOutput`, `InputRejected` | `failed` | message `str(error)` |
| `FactsDeferred` | `waiting` | message `<error> (back in inbox/)` |
| `FetchSkipped` | `would_fetch` | message `str(error)` |
| `FactsUnavailable` | `deferred` | message `str(error)` |
| anything else | `failed` | message `unexpected <TypeName>: <error>` |

- [ ] **Step 1: Write the failing test.** `test_each_exception_maps_to_its_outcome` (parametrized over the table, asserting `kind`, `backend`, `budget`, `config_error`, `message`), `test_a_budget_error_is_not_classified_as_a_plain_usage_limit` (subclass order), `test_facts_deferred_and_fetch_skipped_are_not_plain_facts_unavailable`, `test_an_unknown_exception_is_a_failed_outcome_naming_its_type`.
- [ ] **Step 2: Run** `uv run pytest tests/component/test_outcome.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `classify` with an ordered `isinstance` chain in `outcome.py`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git add src/catcher/modules/pipeline/outcome.py tests/component/test_outcome.py` / `feat: classify exceptions into outcomes`.

### Task 2: `order_notes` and `split_duplicates` as pure functions

**Files:**
- Create: `src/catcher/modules/pipeline/steps.py`
- Test: `tests/component/test_steps.py`

**Interfaces:**
- Consumes: `Note`, `is_snapshot_of` from `inbox.py`.
- Produces: `def order_notes(notes: list[Note]) -> list[Note]` (sort key today: `(str(fm["captured"]), doc_id, path.as_posix())`); `def split_duplicates(ordered: list[Note]) -> tuple[list[Note], list[tuple[Note, Note]]]` returning `(to_process, [(duplicate, winner), ...])`. The winner of a `doc_id` is its longest body (first one on a tie); a non-winner is a duplicate only if `is_snapshot_of(note.body, winner.body)`.

- [ ] **Step 1: Write the failing test.** `test_notes_are_ordered_by_captured_date_then_id_then_path`; `test_the_longest_clip_wins_and_earlier_snapshots_are_duplicates` (3 clips of one chat); `test_a_clip_with_other_content_and_the_same_id_is_processed_not_a_duplicate`; `test_split_does_not_touch_the_files` (tmp repo, files unchanged).
- [ ] **Step 2: Run** `uv run pytest tests/component/test_steps.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** by moving the two loops out of `_run` (`run.py`, the `ordered = sorted(...)` and `winners` blocks) into `steps.py`. Do not edit `_run` yet (Task 6).
- [ ] **Step 4: Run** the same command plus `uv run pytest tests/integration/git -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: pure order_notes and split_duplicates`.

### Task 3: `load_staged_note` keeps the subfolder for `output/`, `archive/`, `failed/`, `duplicates/`

**Files:**
- Modify: `src/catcher/modules/pipeline/inbox.py` (next to `read_note`, `inbox_root`)
- Test: `tests/component/test_inbox.py`

**Interfaces:**
- Produces: `STAGE_FOLDERS = ("inbox", "output", "archive", "failed", "duplicates")`; `def load_staged_note(ideas_repo: Path, path: Path, now: datetime | None = None) -> Note`. It reads `path` (inside `ideas_repo/<folder>/`), sets `note.inbox_rel` to the path **below that folder** (so `notes/<name>.md`), keeps `note.path = path`, and trusts `calculated_filename` only through the existing validation. Raises `ValueError` if `path` is not inside one of the five folders.
- Why: `read_note` finds the subfolder through an `inbox` ancestor, so a file under `output/` loses `notes/` or `clippings/` and `output_path` comes out wrong (design review, B0 item 11).

- [ ] **Step 1: Write the failing test.** `test_a_note_loaded_from_output_keeps_its_subfolder` (create via `start_work`, load the `output/` file, assert `rel == Path("notes/<name>")` and `output_path(repo) == the same file`); same for `archive/` and `failed/`; `test_a_path_outside_the_five_folders_is_refused`.
- [ ] **Step 2: Run** `uv run pytest tests/component/test_inbox.py -q -k staged`. Expected: FAIL.
- [ ] **Step 3: Implement** with `path.resolve().relative_to(ideas_repo.resolve())`, first part must be in `STAGE_FOLDERS`; build the note with `_analyse` and set `inbox_rel`.
- [ ] **Step 4: Run** `uv run pytest tests/component tests/unit -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: load a staged note with its subfolder`.

### Task 4: process in three steps, `process_note` composes them

**Files:**
- Modify: `src/catcher/modules/pipeline/process.py`
- Test: `tests/component/test_process_steps.py`

**Interfaces:**
- Produces (all in `process.py`; `_reason` becomes `ask_llm`, public):
  - `def get_facts(note: Note, svc: Services, opts: ProcessOptions) -> YoutubeFacts | None`: for `youtube` it does what `_process_youtube` does before the LLM (video id, `facts_for`, raise `FactsUnavailable` for an unavailable video or no transcript); for `youtube-gemini` it raises `FactsUnavailable` when `gemini_video_id` finds nothing and returns `None`; for text classes returns `None`.
  - `def ask_llm(note: Note, svc: Services, profile_name: str, facts: YoutubeFacts | None = None) -> LlmResult` (empty and over-long input still raise `InputRejected`).
  - `def build_page(note: Note, svc: Services, result: LlmResult, facts: YoutubeFacts | None) -> ProcessedPage`: tags, checks, render, validate, per class.
- `process_note(note, svc, opts)` keeps its signature and becomes `resolve_profile`, `check_not_blocked`, `get_facts`, `ask_llm`, `build_page`.

- [ ] **Step 1: Write the failing test.** For each of the four classes (`note`, `ai-chat`, `youtube` with the `yt_facts` fixture, `youtube-gemini`) with the fake backend: `test_the_three_steps_give_the_same_page_as_process_note` (page, filename, problems equal); `test_get_facts_is_none_for_text_classes`; `test_build_page_needs_no_backend` (run `build_page` with a `LlmResult` made by the `make_result` fixture, backend factory that raises if called).
- [ ] **Step 2: Run** `uv run pytest tests/component/test_process_steps.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** by splitting `_process_text`, `_process_youtube`, `_process_youtube_gemini`; keep the log lines.
- [ ] **Step 4: Run** `uv run pytest tests/unit tests/component tests/integration/git -q`. Expected: all PASS, no existing test edited.
- [ ] **Step 5: Commit** `refactor: process a note in three callable steps`.

### Task 5: a `Gate` protocol

**Files:**
- Modify: `src/catcher/modules/youtube/gate.py`, `src/catcher/modules/youtube/access.py`
- Test: `tests/component/test_youtube_access.py`

**Interfaces:**
- Produces in `gate.py`: `class Gate(Protocol): def peek(self) -> Wait | None; def reserve(self) -> Wait | None; def record_success(self, started_at: float | None = None) -> None; def record_block(self, started_at: float | None = None) -> float`. `YoutubeGate` satisfies it unchanged. `YoutubeAccess.__init__(self, fetch: FactsFetcher, gate: Gate, ...)`.

- [ ] **Step 1: Write the failing test.** `test_access_works_with_any_gate_that_follows_the_protocol`: a small in-memory `FakeGate` class (open, then closed after `record_block`) drives `YoutubeAccess.get` through success, a 429 (raises `FactsDeferred`) and a closed gate, with no files created.
- [ ] **Step 2: Run** `uv run pytest tests/component/test_youtube_access.py -q -k protocol`. Expected: FAIL (pyright also complains once the type is changed first; run `uv run pyright`).
- [ ] **Step 3: Implement** the Protocol and the changed annotation.
- [ ] **Step 4: Run** `uv run pytest tests/unit tests/component -q` and `uv run pyright`. Expected: PASS, 0 errors.
- [ ] **Step 5: Commit** `refactor: a Gate protocol for the YouTube access`.

### Task 6: `run_pipeline` becomes an orchestrator over the steps

**Files:**
- Modify: `src/catcher/modules/pipeline/run.py`
- Test: `tests/component/test_run_outcomes.py` (new); the existing `tests/integration/git/test_run.py` and `tests/unit/test_missing_paths.py` stay as they are.

**Interfaces:**
- Consumes: `classify`, `Outcome` (Task 1), `order_notes`, `split_duplicates` (Task 2), `process_note` (Task 4).
- Produces in `run.py`: `@dataclass class RunState: blocked: set[str]; budget_blocked: dict[str, int]; attempted: int; seen_ids: set[str]`; `def apply_outcome(ideas: Path, note: Note, outcome: Outcome, state: RunState, item: ItemReport, *, dry_run: bool) -> list[Path]` which sets `item.status`/`item.message`, updates `state` (blocks the backend, counts budget-blocked notes, gives back an attempt for `waiting`) and does the file effect (`mark_deferred` for `deferred`, `move_to_failed` for `failed`, `return_to_inbox` for `waiting` and `interrupted`, nothing for `would_fetch`), returning the touched idea-bucket paths. Message rule for a usage limit: `budget reached (<b>)` if `<b>` is already in `state.budget_blocked`, else the outcome's message.

- [ ] **Step 1: Write the failing test.** In `test_run_outcomes.py`, with a tmp repo and `start_work`: one test per `OutcomeKind` asserting item status/message, where the file is afterwards (output with `stage: deferred`, `failed/` with `.error.txt`, back in `inbox/`, untouched) and the returned paths; `test_a_second_notes_usage_limit_on_a_budget_blocked_backend_says_budget_reached`; `test_a_waiting_outcome_gives_the_attempt_back`; `test_dry_run_changes_no_file_for_any_kind`.
- [ ] **Step 2: Run** `uv run pytest tests/component/test_run_outcomes.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `RunState` and `apply_outcome`, then replace the `except` ladder and the sorting/duplicate blocks in `_run` by `classify`, `apply_outcome`, `order_notes`, `split_duplicates`. Keep the order of operations, log lines and the `finally` that records saved facts.
- [ ] **Step 4: Run** the whole Stage A suite `uv run pytest tests/unit tests/component tests/integration/git -q`. Expected: PASS with **no test file edited**, then `uv run ruff check . && uv run pyright`.
- [ ] **Step 5: Commit** `refactor: run_pipeline orchestrates classify and apply_outcome`.

---

## B1: the database

### Task 7: dependencies, a real Postgres in tests, time helpers

**Files:**
- Modify: `pyproject.toml` (deps `sqlalchemy>=2.0`, `psycopg[binary]>=3.2`, `alembic>=1.13`; dev `testcontainers[postgres]>=4.8`; marker `db`), `src/catcher/core/config.py` (`database_url: str = "postgresql+psycopg://catcher:catcher@localhost:5432/catcher"`), `.env.example`
- Create: `src/catcher/core/db.py`, `tests/integration/db/conftest.py`
- Test: `tests/unit/test_db_helpers.py`, `tests/integration/db/test_fixture.py`

**Interfaces:**
- Produces in `core/db.py`: `def make_engine(url: str) -> Engine`; `@contextmanager def session_scope(engine: Engine) -> Iterator[Session]` (commit on success, roll back on error); `def require_aware(moment: datetime) -> datetime` (raises `ValueError("naive datetime")`, returns it unchanged otherwise); `def utc_now() -> datetime`.
- Produces in `tests/integration/db/conftest.py`: session fixture `pg_engine` (starts `pgvector/pgvector:pg17` through testcontainers, runs `alembic upgrade head` once Task 8 exists, skips with the reason "Docker is not running" when the daemon is unreachable); function fixture `session` (a `session_scope` that truncates every table after the test); a `clock` fixture: a callable frozen at `2026-10-02T12:00:00+00:00` with `.advance(seconds)`.

- [ ] **Step 1: Write the failing test.** `test_require_aware_rejects_a_naive_datetime`, `test_session_scope_commits_and_rolls_back`, and `test_the_database_fixture_answers_select_1_on_postgres_17` (`select version()` contains `PostgreSQL 17`).
- [ ] **Step 2: Run** `uv add sqlalchemy "psycopg[binary]" alembic && uv add --dev "testcontainers[postgres]"`, then `uv run pytest tests/unit/test_db_helpers.py tests/integration/db -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the helpers and the fixtures; register the `db` marker; no model needed yet (the upgrade call in the fixture is added in Task 8).
- [ ] **Step 4: Run** the same command. Expected: PASS (the Postgres test needs Docker running).
- [ ] **Step 5: Commit** `feat: database dependencies, helpers and a real-Postgres test fixture`.

### Task 8: the schema and the first Alembic migration

**Files:**
- Create: `src/catcher/modules/queue/__init__.py`, `src/catcher/modules/queue/models.py`, `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_initial.py`
- Modify: `src/catcher/cli.py` (a `db` command group: `catcher db upgrade [REVISION=head]`, `catcher db downgrade [REVISION=base]`, reading `Settings().database_url`)
- Test: `tests/integration/db/test_schema.py`

**Interfaces:**
- Produces in `models.py` (SQLAlchemy 2 declarative, all timestamps `timestamptz`): `Base`, and tables

  - `jobs`: `id uuid pk`, `type text`, `status text` (CHECK in the five values), `priority int default 0`, `run_after timestamptz`, `reason text null`, `params jsonb`, `result jsonb null`, `error text null`, `attempts int default 0`, `max_attempts int default 3`, `locked_by text null`, `lease_until timestamptz null`, `heartbeat_at timestamptz null`, `dedupe_key text null`, `resource text null`, `created_at`, `started_at null`, `finished_at null`. Partial UNIQUE index on `dedupe_key` `WHERE dedupe_key IS NOT NULL AND status IN ('queued','running')`. Index for the claim on `(priority DESC, run_after, created_at) WHERE status = 'queued'`.
  - `job_items`: `id uuid pk`, `calculated_name text UNIQUE NOT NULL` (`<subfolder>/<name>.md`), `doc_id text`, `doc_class text`, `origin text default 'inbox'` (`inbox` or `backfill`), `status text` (CHECK: `staging`, `waiting_youtube`, `waiting_llm`, `ready`, `published`, `deferred`, `stuck`, `failed`, `duplicate`), `stage_reason text null`, nullable `inbox_path`, `archive_path`, `output_path`, `failed_path`, `docs_page`, `original_filename`, `llm_profile`, `llm_backend`, `llm_model`, `prompt_version`, `tokens_in`, `tokens_out`, `llm_duration_ms`, `llm_result jsonb null`, `warnings jsonb null`, `error text null`, `root_job_id uuid null` (FK `jobs.id`), `created_at` (also "first seen"), `updated_at`.
  - `job_events`: `id bigserial pk`, `job_id uuid null` FK, `item_id uuid null` FK, `ts timestamptz`, `level text` (CHECK `info`, `warning`, `error`), `message text`, `data jsonb null`.
  - `resources`: `name text pk`, `next_allowed_at timestamptz null`, `blocked_until timestamptz null`, `blocked_at timestamptz null`, `streak int default 0`, `concurrency int default 1`, `updated_at timestamptz`.
  - `schedules`: `name text pk`, `last_fired_at timestamptz null`.

- [ ] **Step 1: Write the failing test.** `test_upgrade_from_empty_creates_the_five_tables`; `test_downgrade_to_base_drops_them`; `test_models_and_migration_agree` (`alembic.autogenerate.compare_metadata` on the upgraded database returns `[]`); `test_a_bad_job_status_is_rejected`; `test_two_items_cannot_share_a_calculated_name`; `test_two_active_jobs_cannot_share_a_dedupe_key_but_a_finished_one_can`; `test_catcher_db_upgrade_runs_from_the_command_line` (CliRunner against the container URL via `DATABASE_URL`).
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_schema.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** models, the hand-checked `0001_initial` migration (`op.create_table` etc., no autogenerate output committed unreviewed), `migrations/env.py` taking the URL from `Settings().database_url` unless `-x url=` is given, and the CLI group; wire the upgrade into the `pg_engine` fixture of Task 7.
- [ ] **Step 4: Run** `uv run pytest tests/integration/db -q`, `uv run pytest tests/unit tests/component -q`, `uv run pyright`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: the Stage B schema and the first migration`.

---

## B2: the queue core

### Task 9: `enqueue` with a dedupe key

**Files:**
- Create: `src/catcher/modules/queue/queue.py`
- Test: `tests/integration/db/test_queue_enqueue.py`

**Interfaces:**
- Consumes: `Job` model (Task 8), `require_aware` (Task 7).
- Produces: `def enqueue(session: Session, *, type: str, now: datetime, run_after: datetime | None = None, priority: int = 0, params: dict[str, Any] | None = None, resource: str | None = None, dedupe_key: str | None = None, max_attempts: int = 3) -> tuple[Job, bool]` returning `(job, created)`. With a `dedupe_key` it uses `INSERT … ON CONFLICT DO NOTHING` on the partial unique index, then selects the active job with that key and returns it with `created=False`. `run_after` defaults to `now`.

- [ ] **Step 1: Write the failing test.** `test_enqueue_creates_a_queued_job_due_now`; `test_a_naive_now_is_rejected`; `test_a_second_enqueue_with_the_same_key_returns_the_first_job`; `test_two_connections_enqueueing_the_same_key_at_once_get_one_job` (two threads with a barrier; one row, both see the same id); `test_the_key_is_free_again_once_the_job_has_finished`.
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_queue_enqueue.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** with the PostgreSQL `insert().on_conflict_do_nothing(index_elements=[Job.dedupe_key], index_where=…)`; no `commit` inside the function (the caller's `session_scope` commits).
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat: queue enqueue with a dedupe key`.

### Task 10: `claim` with `SKIP LOCKED`, priority and `run_after`

**Files:**
- Modify: `src/catcher/modules/queue/queue.py`
- Test: `tests/integration/db/test_queue_claim.py`

**Interfaces:**
- Produces: `def claim(session: Session, *, worker: str, now: datetime, lease_s: float, types: Sequence[str] | None = None) -> Job | None`. Picks one `queued` job with `run_after <= now` (optionally of the given types), `ORDER BY priority DESC, run_after, created_at`, `FOR UPDATE SKIP LOCKED LIMIT 1`; sets `status='running'`, `locked_by=worker`, `attempts += 1`, `started_at` (first claim only), `heartbeat_at=now`, `lease_until=now+lease_s`.

- [ ] **Step 1: Write the failing test.** `test_claim_returns_none_when_nothing_is_due`; `test_a_job_with_a_future_run_after_is_invisible_until_its_time` (frozen clock, advance); `test_higher_priority_goes_first_then_earlier_run_after_then_earlier_created_at`; `test_claim_sets_the_lease_and_counts_the_attempt`; `test_two_workers_claiming_at_once_never_get_the_same_job` (10 jobs, 4 threads each claiming in a loop with its own session; the union of claimed ids has no duplicate and equals the 10); `test_types_filter`.
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_queue_claim.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** with `select(Job).where(...).order_by(...).with_for_update(skip_locked=True).limit(1)` and an in-place update.
- [ ] **Step 4: Run** the same command 5 times in a row to check the concurrency test is stable. Expected: PASS each time.
- [ ] **Step 5: Commit** `feat: queue claim with skip locked and priorities`.

### Task 11: `heartbeat`, `complete`, `fail`, `defer` with fencing

**Files:**
- Modify: `src/catcher/modules/queue/queue.py`
- Test: `tests/integration/db/test_queue_finish.py`

**Interfaces:**
- Produces (each takes the `Job` returned by `claim`; each returns `bool`, `False` when the job is no longer the caller's, i.e. the `UPDATE … WHERE id=:id AND status='running' AND locked_by=:worker AND attempts=:attempts` matched no row):
  - `def heartbeat(session: Session, job: Job, *, now: datetime, lease_s: float) -> bool`
  - `def complete(session: Session, job: Job, *, now: datetime, result: dict[str, Any] | None = None) -> bool` → `succeeded`, `finished_at`, `result`.
  - `def fail(session: Session, job: Job, *, now: datetime, error: str) -> bool` → `failed`, `error`, no retry.
  - `def defer(session: Session, job: Job, *, now: datetime, run_after: datetime, reason: str) -> bool` → `queued`, `run_after`, `reason`, lock cleared, **`attempts` given back** (a deferral is not a failure).

- [ ] **Step 1: Write the failing test.** `test_complete_marks_the_job_succeeded`; `test_fail_marks_it_failed_with_the_error_and_does_not_requeue`; `test_defer_requeues_with_a_future_run_after_and_does_not_count_an_attempt`; `test_heartbeat_extends_the_lease`; `test_a_stale_worker_cannot_complete_a_job_another_worker_has_reclaimed` (claim as `w1`, advance past the lease, `reap`—Task 12 stub is not available, so set the row back with a plain SQL update to `queued`, claim as `w2`, then `complete` as `w1` returns `False` and the job is still `running` for `w2`); `test_complete_on_a_job_that_is_not_running_returns_false`.
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_queue_finish.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the four functions with the guarded `UPDATE … RETURNING`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat: queue heartbeat, complete, fail and defer with fencing`.

### Task 12: `reap` recovers crashed jobs and stops poison jobs

**Files:**
- Modify: `src/catcher/modules/queue/queue.py`
- Test: `tests/integration/db/test_queue_reap.py`

**Interfaces:**
- Produces: `def reap(session: Session, *, now: datetime) -> list[Job]`: every `running` job with `lease_until < now` is returned to `queued` (`run_after=now`, lock cleared, `reason="lease expired"`) if `attempts < max_attempts`, else set to `failed` with `error="lease expired too often (<attempts> attempts)"`. Returns the jobs it changed. It is meant to run at worker start and on a timer.

- [ ] **Step 1: Write the failing test.** `test_a_job_whose_lease_has_expired_comes_back_to_the_queue`; `test_a_job_with_a_live_lease_is_left_alone` (heartbeat keeps it); `test_a_poison_job_ends_failed_after_max_attempts` (claim, advance past lease, reap, repeat; third reap leaves `failed` and a fourth claim finds nothing); `test_the_reaped_job_is_claimable_by_another_worker_and_the_old_worker_is_refused` (ties Task 11 to the real reaper).
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_queue_reap.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** with one `UPDATE … WHERE status='running' AND lease_until < :now RETURNING`.
- [ ] **Step 4: Run** `uv run pytest tests/integration/db -q`, `uv run pytest tests/unit tests/component tests/integration/git -q`, `uv run ruff check .`, `uv run pyright`. Expected: all PASS.
- [ ] **Step 5: Commit** `feat: queue reaper for expired leases and poison jobs`.

### Task 13: docs and the size check

**Files:**
- Modify: `docs/idea-catcher-how-to-run.md` (a "Database (Stage B)" section: `docker run` of the Postgres image, `DATABASE_URL`, `uv run catcher db upgrade`, running the database tests), `docs/idea-catcher-configuration.md` (`DATABASE_URL`), `docs/idea-catcher-service-architecture.md` (B0–B2 marked built with the date; the line count of `queue.py`), `README.md` (command table: `catcher db upgrade`)

- [ ] **Step 1: Count** `wc -l src/catcher/modules/queue/queue.py`. If it is over about 300 lines, or a concurrency test is flaky, write that down in the architecture doc under the Procrastinate fallback rule (decision 11) and tell the user before starting B3.
- [ ] **Step 2: Write** the doc changes. Check any new Mermaid blocks with the validator.
- [ ] **Step 3: Run** `uv run pytest tests/unit tests/component tests/integration -q`. Expected: PASS.
- [ ] **Step 4: Commit** `docs: Stage B0-B2 built`.
