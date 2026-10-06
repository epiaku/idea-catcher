# Stage B5: Item States, Metrics and LLM Blocks in Postgres Implementation Plan

**Status: built 2026-10-06** (Tasks 1-7 on `stage-b`; what changed from this plan is in the architecture doc, "Built (2026-10-06): B5").

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the worker path, Postgres holds the truth for every document's state, the LLM metrics, the events and the LLM backend blocks; the documents in `output/` show that state in their frontmatter; `stuck` is detected; `catcher reconcile` rebuilds the database from the folders.

**Architecture:** One small service, `ItemStates`, changes an item's status in the database (with a `stage_since`, a reason and a `job_events` row) in one commit and then, after the commit, mirrors the state into the working copy's frontmatter (`stage`, `stage_reason`, `stage_since`); the database is the truth and the mirror is best effort. The in-memory `BackendBlocks` becomes a Postgres-backed one on the `resources` table. A periodic step in the worker's reap timer marks long-deferred items `stuck`.

**Tech Stack:** Python 3.12, sync SQLAlchemy 2 + psycopg 3, Alembic, pytest with a real Postgres 17 (testcontainers), `scripts/check`.

**Spec:** `docs/idea-catcher-service-architecture.md` (step B5, Stage B decisions 3, 7, 8, 9, 10, "Open items after B3/B4/B4b", the `job_items` and `job_events` tables in "Database & Metrics"); the user's direction of 2026-10-04: Postgres is the single truth; no mode that works with or without it. The Stage A `run pipeline` command becomes a thin wrapper over the worker path in a LATER plan (B5b, written after this one); this plan does not touch the old loop.

## Defaults I chose (change any before we start)

1. **Scope: the worker path only.** The Stage A loop (`run_pipeline()`, `run pipeline`) still keeps its states in folders and frontmatter until B5b replaces it with a wrapper over the worker. B5b also decides what happens to `--dry-run` (decision 12: dry runs never go through the queue).
2. **One writer of item state.** `ItemStates.transition(...)` is the only code that changes `job_items.status` from B5 on (the handlers call it; `set_item_status` stays as the low-level function it uses). Each transition writes: the new status, `stage_reason`, `stage_since` (only when the status really changes), `updated_at`, and one `job_events` row (level info, warning or error).
3. **The frontmatter mirror** is written on status changes only, to the working copy in `output/` (or the file in `failed/`): `stage`, `stage_reason`, `stage_since`. Values are the nine item statuses. The finished page keeps `stage: published` and `created_by` as built (not `stage_since`: the page equals the docs page, and its expected test pages would change again). Reconcile and the readers also understand the Stage A names (`analyzed`, `deferred_at`, `deferred_reason`). A failed mirror write is logged and never fails the job: the next transition rewrites it.
4. **`stuck`:** an item that has been `deferred` for more than `STUCK_AFTER_DAYS` (default 3) becomes `stuck` (a warning event). It keeps being retried: `retry_deferred` picks up `deferred` AND `stuck` items; a successful run publishes it and the status becomes `published`.
5. **Retries add up like this (documented, not new code):** inside one `llm.reason` job the model call is retried by the LLM service (the in-call attempts); across jobs there is NO job-level retry layer (decision 3): a deferred item waits for `pipeline.run` with `retry_deferred` (the scheduler adds that in B6). The only waiting time is the backend block.
6. **LLM backend blocks in Postgres.** Rows in `resources` named by backend (`openai`, `freellmapi`, `fake`...); a missing row means OPEN (unlike `youtube`, where missing means closed: an LLM block is only ever created by a failure). The block length stays `LLM_BLOCK_S` (600 s); a used-up budget keeps the 6 h `budget_retry_delay` idea as `LLM_BUDGET_BLOCK_S` (default 21600). A wrong model (an HTTP 4xx that is not auth/quota) blocks the PROFILE, not the backend: row name `<backend>:<profile>`. The block carries a `reason` (new column) shown by `catcher jobs list` style tools.
7. **Metrics:** `llm.reason` writes `job_items.llm_profile`, `llm_backend`, `llm_model`, `prompt_version`, `tokens_in`, `tokens_out`, `llm_duration_ms`, `warnings` (dropped tags) and `docs_page`; a reply served from a saved reply is recorded as such (`llm_result.saved = true`, tokens kept but flagged, so cost queries can exclude them). `job_events` get the state changes and the warnings.
8. **`catcher reconcile [--dry-run] [--keep-gate]`:** rebuilds missing `job_items` rows from the idea-bucket folders and frontmatter (calculated name, class, status by folder and `stage`), fixes the status of rows whose file moved (never deletes a row, never deletes a file), and CLOSES the `youtube` gate for `YOUTUBE_BLOCK_HOURS` (decision 10: a rebuild may have lost a block) unless `--keep-gate`. It needs the database and takes the run lock.
9. **`catcher items list [--status S] [--limit N]`:** one line per item (name, class, status, since, reason) so `stuck` is visible without SQL. Read-only.
10. **Not in B5:** the `run pipeline` wrapper and retiring the old loop (B5b), the scheduler (B6), the API (Stage C).

## Global Constraints

- ruff line length 110; pyright standard on `src`; `scripts/check` passes at the end of every task (once per task).
- Sync SQLAlchemy 2 and psycopg 3; every function takes its time from Python (an injected `clock` or an aware `now`), never SQL `now()` (decision 8).
- No live YouTube and no live LLM in any test; the network guard is on by default in every pytest run (`tests/conftest.py`); NEVER set `CATCHER_ALLOW_NETWORK`; never write a test (not even a RED run) that would make a real outside connection.
- Never modify `tests/data` or `tmp/ic`; the user's container `catcher-db` on port 5432 must not be stopped or touched; run ONE pytest session at a time; never use `timeout` (not installed); unit and component tests must not need Docker.
- Commit locally on `stage-b`; do not push; the pre-commit hook must pass (no `--no-verify`); it stashes unstaged changes but leaves untracked files in place: commit the group that owns a new untracked test file first.

## Review Focus

1. **The mirror write fails** (read-only folder, file gone): the transition is committed and the job still succeeds; the next transition rewrites the mirror. Task 2.
2. **A status change that changes nothing** (a handler runs twice): no new `stage_since`, no duplicate event, no frontmatter rewrite. Task 1/2.
3. **`stuck` never fires early or twice:** 2 days 23 h stays `deferred`; 3 days and a bit becomes `stuck` once; a stuck item that is retried and defers again stays `stuck` (does not reset the clock). Task 4.
4. **Two workers/engines blocking the same backend at once** and a block that expires: one row, the later end wins, expiry reopens; a Postgres outage during the check treats the backend as blocked for one poll, never as open for a down backend. Task 5.
5. **Reconcile on a half-broken state:** a file in `output/` with no row, a row whose file moved to `failed/`, a duplicate name, a damaged frontmatter: no crash, nothing deleted, a clear report, idempotent (a second run changes nothing). Task 6.
6. **`retry_deferred` and `stuck`:** a stuck item is picked up by `retry_deferred` and requeue treats it like a deferred one. Task 4.

---

## File structure

| File | Responsibility |
| --- | --- |
| `migrations/versions/0005_item_stage_and_block_reason.py` (new) | `job_items.stage_since`; `resources.reason`. |
| `src/catcher/modules/queue/states.py` (new) | `ItemStates`: the one writer of item state (DB + event + mirror). |
| `src/catcher/modules/pipeline/mirror.py` (new) | Read/write the frontmatter mirror of a working copy (`stage`, `stage_reason`, `stage_since`; understands the Stage A names). |
| `src/catcher/modules/worker/handlers_pipeline.py` | Handlers call `ItemStates.transition`; `llm.reason` writes the metrics. |
| `src/catcher/modules/worker/blocks.py` | `BackendBlocks` becomes Postgres-backed (same interface). |
| `src/catcher/modules/worker/loop.py` | The reap timer also runs `mark_stuck`. |
| `src/catcher/modules/queue/reconcile.py` (new) | The reconcile logic (pure-ish, takes a session and the folders). |
| `src/catcher/cli.py` | `catcher reconcile`, `catcher items list`. |
| `src/catcher/core/config.py` | `STUCK_AFTER_DAYS`, `LLM_BUDGET_BLOCK_S`. |

---

### Task 1: The item state writer

**Files:**
- Create: `migrations/versions/0005_item_stage_and_block_reason.py`, `src/catcher/modules/queue/states.py`
- Modify: `src/catcher/modules/queue/models.py` (`JobItem.stage_since`, `Resource.reason`), `tests/integration/db/conftest.py` only if the re-seed helper needs the new column
- Test: `tests/integration/db/test_item_states.py` (new), `tests/integration/db/test_schema.py` (migration test)

**Interfaces:**
- Produces: migration `0005` adds `job_items.stage_since timestamptz NULL` (existing rows get `updated_at`) and `resources.reason text NULL`; `class ItemStates` with `__init__(self, *, mirror: Callable[[JobItem], None] | None = None)` and `transition(session: Session, calculated_name: str, status: str, *, now: datetime, reason: str | None = None, job_id: uuid.UUID | None = None, level: str = "info", data: dict[str, Any] | None = None) -> JobItem | None` (None when the row does not exist): validates the status against `ITEM_STATUSES`, requires an aware `now`, changes the row only when the status or the reason changes, sets `stage_since = now` only when the STATUS changes, always sets `updated_at`, writes one `JobEvent` (message `"<calculated_name>: <old> -> <new>"` plus the reason, `level` as given) when something changed, and returns the item; the `mirror` callable is NOT called inside the session (the caller calls `states.mirror_after_commit(item)` after its commit).
- Consumes: `set_item_status`, `JobItem`, `JobEvent`, `ITEM_STATUSES` from `queue/items.py` and `queue/models.py`.

- [ ] **Step 1: Write the failing tests:** `test_upgrade_adds_stage_since_and_reason_and_downgrade_removes_them` (existing item rows get `stage_since = updated_at`); `test_transition_changes_status_reason_and_stage_since_and_writes_an_event`; `test_a_second_identical_transition_changes_nothing_and_writes_no_event` (Review Focus 2); `test_a_change_of_only_the_reason_keeps_stage_since`; `test_transition_of_a_missing_item_returns_none`; `test_a_bad_status_or_a_naive_now_is_rejected`; `test_the_event_carries_the_job_id_and_level`.
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_item_states.py tests/integration/db/test_schema.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the migration, the two columns on the models (and the models-vs-migration agreement test must still pass) and `ItemStates`.
- [ ] **Step 4: Run** the same command, then `uv run pytest tests/integration/db -q` (one session). Expected: PASS.
- [ ] **Step 5: Commit** `feat: one writer for item state, with stage_since and an event`.

### Task 2: The frontmatter mirror and the handlers use the writer

**Files:**
- Create: `src/catcher/modules/pipeline/mirror.py`
- Modify: `src/catcher/modules/worker/handlers_pipeline.py` (every `set_item_status` call becomes `ItemStates.transition`, then the mirror after the commit), `src/catcher/modules/worker/loop.py` (the reaper's `fail_items_of`), `src/catcher/modules/pipeline/inbox.py` (`mark_deferred` and `start_work` write the new names), `src/catcher/modules/worker/app.py` (the context carries one `ItemStates`)
- Test: `tests/component/test_mirror.py` (new, no Docker), `tests/integration/db/test_handler_*.py` (extend), `tests/integration/db/test_worker_crashes.py` (extend)

**Interfaces:**
- Produces: `write_mirror(path: Path, *, stage: str, reason: str | None, since: datetime) -> bool` (rewrites only the three frontmatter keys of a markdown file, keeps the body and every other key, atomic write, returns False when the file does not exist) and `read_mirror(path: Path) -> MirrorState | None` (`MirrorState(stage: str | None, reason: str | None, since: datetime | None)`, understanding `analyzed`, `deferred`, `deferred_at`, `deferred_reason` from Stage A); `HandlerContext.item_states: ItemStates` whose `mirror` writes into `ctx.ideas / "output" / <calculated_name>` (or `failed/`).
- Consumes: Task 1.

- [ ] **Step 1: Write the failing tests:** component `test_write_mirror_adds_the_three_keys_and_keeps_everything_else`, `test_write_mirror_of_a_missing_file_returns_false`, `test_read_mirror_understands_the_stage_a_names`, `test_a_damaged_frontmatter_is_not_overwritten_and_reports_false`; db handler tests: `test_every_status_change_of_the_handlers_goes_through_the_writer` (waiting_llm -> published; waiting_youtube; deferred with its reason; failed; the item row, the event and the working copy's frontmatter agree after each), `test_a_failing_mirror_does_not_fail_the_job` (Review Focus 1: the output folder made read-only or the file removed; the transition is committed, the job `succeeded`, a warning logged), `test_the_next_transition_repairs_the_mirror`, `test_reaper_failed_items_get_the_mirror_in_failed` (the crash tests).
- [ ] **Step 2: Run** the new tests. Expected: FAIL.
- [ ] **Step 3: Implement** `mirror.py`; replace the handler and reaper calls; `start_work` writes `stage: staging`/`stage_since` instead of `stage: analyzed` ONLY if no Stage A test pins `analyzed` (otherwise keep `analyzed` for the Stage A loop and let the worker write the new names after `start_work`).
- [ ] **Step 4: Run** `uv run pytest tests/unit tests/component tests/integration -q` (one session). Expected: PASS; Stage A tests unchanged.
- [ ] **Step 5: Commit** `feat: item states are mirrored into the working copy's frontmatter`.

### Task 3: Metrics and events

**Files:**
- Modify: `src/catcher/modules/worker/handlers_pipeline.py` (`llm.reason` writes the metrics), `src/catcher/modules/queue/states.py` (a `record_metrics` helper)
- Test: `tests/integration/db/test_handler_llm_reason.py` (extend)

**Interfaces:**
- Produces: after a successful `llm.reason` the item row holds `llm_profile`, `llm_backend`, `llm_model`, `prompt_version`, `tokens_in`, `tokens_out`, `llm_duration_ms`, `warnings` (list of strings, e.g. the dropped tags), `docs_page` (the page's path relative to the docs repo) and `llm_result` = `{"attempts": n, "saved": bool}`; for a deferred or failed outcome the row keeps `error` (existing) and gets the profile/backend that was tried.

- [ ] **Step 1: Write the failing tests:** `test_a_published_item_has_its_llm_metrics`, `test_a_saved_reply_is_flagged_and_keeps_the_recorded_tokens`, `test_dropped_tags_are_recorded_as_warnings_and_an_event`, `test_a_deferred_item_records_the_backend_that_was_tried`, `test_the_metrics_survive_a_rerun_without_duplicating_events`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** `record_metrics` and call it in the same transaction as the item's `published` transition.
- [ ] **Step 4: Run** `uv run pytest tests/integration/db -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: llm.reason records the model, tokens, warnings and page on the item`.

### Task 4: `stuck`, `retry_deferred` and `catcher items list`

**Files:**
- Modify: `src/catcher/modules/worker/loop.py` (the reap timer also calls `mark_stuck`), `src/catcher/modules/queue/states.py` (`mark_stuck`), `src/catcher/modules/worker/handlers_pipeline.py` (`retry_deferred` and `_requeue` treat `stuck` like `deferred`), `src/catcher/cli.py` (`items list`), `src/catcher/core/config.py` (`stuck_after_days`)
- Test: `tests/integration/db/test_stuck.py` (new), `tests/integration/db/test_items_cli.py` (new)

**Interfaces:**
- Produces: `mark_stuck(session: Session, *, now: datetime, after_days: float) -> list[str]` (the calculated names of items whose status is `deferred` and `stage_since <= now - after_days`; each goes through `ItemStates.transition(..., "stuck", reason="deferred for <n> days: <old reason>", level="warning")`; the mirror runs after the commit); `Settings.stuck_after_days: float = 3` (`gt=0`); `catcher items list [--status S] [--limit N]` prints `<calculated_name>  <class>  <status>  <since>  <reason>` newest first.

- [ ] **Step 1: Write the failing tests:** `test_an_item_deferred_for_less_than_the_limit_stays_deferred`, `test_an_item_deferred_longer_becomes_stuck_once_with_a_warning_event` (Review Focus 3), `test_a_stuck_item_that_defers_again_stays_stuck_and_keeps_its_clock`, `test_retry_deferred_picks_up_stuck_items_too_and_a_publish_ends_it` (Review Focus 6), `test_the_reap_timer_runs_mark_stuck_and_an_error_in_it_does_not_stop_the_reaper`, `test_items_list_shows_stuck_items`, `test_items_list_filters_by_status`, `test_items_list_exits_2_without_the_database`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the pieces above.
- [ ] **Step 4: Run** `uv run pytest tests/integration/db -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: items deferred for 3 days become stuck, and catcher items list shows them`.

### Task 5: LLM backend blocks in Postgres

**Files:**
- Modify: `src/catcher/modules/worker/blocks.py` (Postgres-backed `BackendBlocks`), `src/catcher/modules/worker/app.py` (build it on the worker engine), `src/catcher/modules/worker/handlers_pipeline.py` (profile-level blocks for a wrong model), `src/catcher/core/config.py` (`llm_budget_block_s`), `src/catcher/modules/queue/models.py` is already extended in Task 1 (`Resource.reason`)
- Test: `tests/integration/db/test_backend_blocks.py` (new; replaces the in-memory unit tests of `BackendBlocks` that move here), `tests/integration/db/test_handler_llm_reason.py` (the F4 tests keep their assertions)

**Interfaces:**
- Produces: `BackendBlocks(engine: Engine, *, clock: Callable[[], datetime])` with `block(key: str, until: datetime, reason: str) -> None` (upsert the `resources` row named `key` keeping the LATER `blocked_until`; `blocked_at` = now), `active(now: datetime) -> dict[str, str]` (key -> reason for rows whose `blocked_until > now`; keys without `:` are backends, keys `<backend>:<profile>` are profile blocks), and `unblock(key)` for tests; a Postgres error in `active` returns every KNOWN backend as blocked for this call (never open) and logs it; the budget block uses `LLM_BUDGET_BLOCK_S` (21600), usage limit and down use `LLM_BLOCK_S` (600), a wrong model (4xx that is not auth or quota) blocks the profile.
- Consumes: `Resource` model, `make_worker_engine`.

- [ ] **Step 1: Write the failing tests:** `test_a_block_survives_a_new_blocks_object_and_a_restart` (a second `BackendBlocks` on the same engine sees it), `test_a_block_expires_with_the_clock`, `test_two_engines_blocking_the_same_backend_keep_one_row_and_the_later_end` (Review Focus 4), `test_a_postgres_error_counts_as_blocked_not_open`, `test_a_budget_block_is_six_hours_and_a_usage_limit_ten_minutes`, `test_a_wrong_model_blocks_the_profile_not_the_backend`, `test_other_profiles_of_the_same_backend_still_run`; the existing F4 handler tests (usage limit blocks the next document; saved reply served while blocked; refresh_llm deferred while blocked; InvalidOutput does not block) pass unchanged.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** and delete the in-memory implementation (its unit tests move).
- [ ] **Step 4: Run** `uv run pytest tests/unit tests/component tests/integration -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: LLM backend blocks live in Postgres and survive a restart`.

### Task 6: `catcher reconcile`

**Files:**
- Create: `src/catcher/modules/queue/reconcile.py`
- Modify: `src/catcher/cli.py` (`reconcile`)
- Test: `tests/integration/db/test_reconcile.py` (new)

**Interfaces:**
- Produces: `reconcile(session: Session, ideas: Path, *, now: datetime, apply: bool) -> ReconcileReport` (`created: list[str]`, `status_fixed: list[tuple[str, str, str]]`, `missing_files: list[str]`, `skipped: dict[str, str]` with reasons); status by folder: a file in `output/` with `stage` waiting_youtube/waiting_llm/deferred/stuck (or the Stage A `analyzed`/`deferred`) -> that status (`analyzed` -> `waiting_llm`), a finished page in `output/` (`stage: published`) -> `published`, a file in `failed/` -> `failed`, in `duplicates/` -> `duplicate`; a row whose file is gone is reported, never deleted; `catcher reconcile [--dry-run] [--keep-gate]` takes the run lock, calls `reconcile`, and unless `--keep-gate` closes the `youtube` gate for `YOUTUBE_BLOCK_HOURS` (through `PostgresGate.record_block`-style closing: use the gate's own closed state) and prints one line saying so; exit 2 without the database or while a worker runs.

- [ ] **Step 1: Write the failing tests:** `test_reconcile_creates_rows_for_files_without_one`, `test_reconcile_fixes_the_status_of_a_row_whose_file_moved_to_failed`, `test_a_row_without_a_file_is_reported_not_deleted`, `test_a_damaged_frontmatter_is_skipped_with_a_reason`, `test_reconcile_is_idempotent` (Review Focus 5), `test_dry_run_changes_nothing`, `test_the_gate_is_closed_unless_keep_gate`, `test_reconcile_after_deleting_the_database` (empty tables, folders from the test data: every document gets its row and status; run on a temporary copy via `reset_test_repos`), `test_reconcile_exits_2_while_a_worker_holds_the_lock`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the module and the command.
- [ ] **Step 4: Run** `uv run pytest tests/integration/db -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: catcher reconcile rebuilds the item rows from the folders`.

### Task 7: Docs

**Files:**
- Modify: `docs/idea-catcher-how-to-run-stage-b.md` (item states and `stage` in the frontmatter, `stuck`, `catcher items list`, `catcher reconcile`, how retries add up, the new settings), `docs/idea-catcher-configuration.md` (`STUCK_AFTER_DAYS`, `LLM_BUDGET_BLOCK_S`, `LLM_BLOCK_S`), `docs/idea-catcher-service-architecture.md` (B5 built with the date; what changed from the plan; the metrics queries with the real column names; update 'Open items'; add the B5b item), `docs/idea-catcher-pipeline.md` (the state list), `README.md` (the new commands), this plan (status line).

- [ ] **Step 1: Verify for real** on your own throwaway Postgres (a random-port `docker run`, never `catcher-db`) and a throwaway copy of the test repos (`catcher testdata reset --target <tmp>`; saved replies, so no model and no YouTube call; `OPENAI_API_KEY=""`, `FREELLMAPI_URL=http://127.0.0.1:1/v1`, `YOUTUBE_OFFLINE=1`): `jobs add pipeline.run`, `worker --once`, open two working copies and show `stage`/`stage_reason`/`stage_since`, `catcher items list`, make an item deferred (empty key and `refresh_llm=true`), fast-forward its `stage_since` in SQL and show `stuck` after the reap, delete the tables (`db downgrade base` then `upgrade`) and run `catcher reconcile`, and show the gate closed with `catcher youtube gate`; then the SQL metrics queries from the architecture doc with the real column names. Record every command that does not work as documented and fix the doc (or report a bug).
- [ ] **Step 2: Write** the doc changes, short, plain words.
- [ ] **Step 3: Run** `scripts/check` once. Expected: all checks pass.
- [ ] **Step 4: Commit** `docs: Stage B5 built`.
