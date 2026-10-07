# Stage B5b: `run pipeline` Over the Worker, and the Old Loop Retired Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `catcher run pipeline` becomes a thin command over the worker path (one code path, one truth in Postgres), the old Stage A loop `run_pipeline()` is deleted, and every behaviour the old loop's tests pinned is either pinned on the worker path or dropped on purpose.

**Architecture:** The command queues one `pipeline.run` job with the user's options as job params, runs the worker in the same process (taking the worker's Postgres lock) until nothing is due, queues one `pipeline.publish` job, and prints a report built from the database (`job_items` of that run). `--dry-run` stays a separate, read-only preview that is extracted from the old loop before the loop is deleted. The tests of the old loop move to the worker path through a helper that builds the same report from the database.

**Tech Stack:** Python 3.12, sync SQLAlchemy 2 + psycopg 3, pytest with a real Postgres 17 (testcontainers), Typer, `scripts/check`.

**Spec:** the user's direction of 2026-10-04 and 2026-10-05: Postgres is the single truth, `run pipeline` becomes a thin wrapper over the worker path; `docs/idea-catcher-service-architecture.md` (Stage B decisions 3, 5, 12; "Open items after B5": the B5b items); the old loop in `src/catcher/modules/pipeline/run.py`; the worker handlers in `src/catcher/modules/worker/handlers_pipeline.py`.

## Defaults I chose (change any before we start)

1. **One path.** After B5b the only code that processes documents is the worker handlers. `run pipeline` is a convenience for people who do not want to type `jobs add` and `worker --once`.
2. **What the command does, in order:** check `DATABASE_URL` (exit 2 as today); take the worker's lock for the whole command (exit 2 when a worker or run holds it); queue `pipeline.run` with the params (`limit`, `only` from `--file`, `requeue`, `retry_deferred`, `profile`, `refresh_llm`, `refresh_facts`); run the worker loop in-process until nothing is due (the same `Worker` class `catcher worker --once` uses); queue `pipeline.publish` (`push` = `--push`, `pull` = true as the old run pulled first) and run it; print the report; exit 0/1/2 as today (1 when a document failed, a name was not found, or a file was unreadable).
3. **The report is built from the database**, not from the old loop: `report_for_job(session, job_id) -> RunReport` reads the `job_items` whose `root_job_id` is the run's `pipeline.run` job, plus the `pipeline.run` job's result counts, plus the documents the run left in the inbox because of `limit`. The printed lines keep today's format (`published      ai-chat  <id>  <name>`, then `summary: {...}`).
4. **`--dry-run` is a preview, not a queued run** (decision 12): it keeps today's meaning (change no files, call no model, ask YouTube nothing, say what a run would do per document: `would_publish`, `would_call_llm`, `would_fetch`, `duplicate`, `artifact`) and is extracted into `pipeline/preview.py` before the old loop is deleted. It still needs the database and the lock (B4b).
5. **`--wait-youtube [seconds]`:** instead of sleeping inside the old loop, the command keeps polling after the drain while fetch jobs of this run are queued and due within `YOUTUBE_WAIT_MAX_S`, then stops; clips whose gate is further away stay queued and the report says `N clip(s) wait for YouTube until HH:MM: run catcher worker`.
6. **`--refresh-facts` becomes a `pipeline.run` param** (`refresh_facts`) copied to the `youtube.fetch` job it queues; the fetch handler then fetches even when facts are saved (through the gate as always).
7. **What may be dropped on purpose** (Task 1 lists every case, the user decides): the per-document live log lines `(1/2) processing ...` (the worker logs its own lines), the in-run per-backend counters of Stage A (the Postgres blocks replace them), and any Stage A behaviour that exists only because the loop was one process.
8. **Tests move, they are not deleted:** the behaviour tests of the old loop (about 170 `run_pipeline(` calls in 8 files) are rewritten to call a helper `run_on_worker(...)` that does what the command does and returns the same `RunReport` shape, so their assertions about folders, pages, statuses and counts stay. They need Docker now (a real Postgres): they move to `tests/integration/db/` or keep their folder and use the db fixtures (the pre-commit hook does not run integration tests).
9. **Not in B5b:** the scheduler (B6), Compose (B7), backfill (B8), the API (Stage C).

## Decisions of the user after the parity table (2026-10-06)

The parity table is `docs/superpowers/plans/2026-10-06-idea-catcher-stage-b5b-parity-table.md` (72 behaviours: 30 covered, 37 add to worker (many are only "port the test"), 5 dropped on purpose). The user decided:
1. **B72 (the report):** the `pipeline.run` handler RETURNS the names of duplicates, artifacts, unreadable files, unmatched `--file`/`--requeue` names, requeue skips and the documents left in the inbox by `--limit` in its job result (JSON lists next to the counts). `report_for_job` and the printed report use them.
2. **B5 (pull):** the command's `pipeline.publish` job has `pull = push`: without `--push` it only commits (no network; works in repos with no remote); with `--push` it pulls (rebase) and pushes. This replaces default 2's `pull = true`.
3. **B19/B20 (budget lines):** after the run the command prints one summary line per blocked backend read from the `resources` table (`openai blocked until 15:40 (budget reached): N document(s) deferred`); the old per-note repeats go. A way to list/lift blocks (`catcher blocks`) is an open item, not in B5b.
4. **B56 (Ctrl-C):** the command puts its own `running` job back to `queued` (no attempt counted), prints `interrupted: N job(s) left queued, run catcher worker to finish`, exits 1 and does NOT run `pipeline.publish`.
5. **Accepted as proposed (informational):** the commit messages lose their counts (B3), the `(1/2) processing` / `processed 2/2` log lines go (B21: the worker logs its own lines), the lock is checked per job and the 'before the artifacts' checkpoint is dropped (B57).

## Global Constraints

- ruff line length 110; pyright standard on `src`; `scripts/check` passes at the end of every task (once per task).
- No live YouTube and no live LLM in any test; the network guard is on by default in every pytest run (`tests/conftest.py`): NEVER set `CATCHER_ALLOW_NETWORK`; never write a test (not even a RED run) that makes a real outside connection.
- Never modify `tests/data` or `tmp/ic`; the user's container `catcher-db` on port 5432 must not be stopped or touched; run ONE pytest session at a time; never use `timeout` (not installed); unit and component tests must not need Docker.
- Commit locally on `stage-b`; do not push; the pre-commit hook must pass (no `--no-verify`); it stashes unstaged changes but leaves untracked files in place: commit the group that owns a new untracked test file first.
- **Lighter review mode (user decision 2026-10-05):** Tasks 3, 5, 6 and 7 are risky (the command, the test migration, the deletion): strict review. Tasks 1, 2 and 4 get one review, no re-review unless an Important finding was fixed. The final fix wave fixes Critical and Important findings only.

## Review Focus

1. **A behaviour that no test pins any more:** after the migration every assertion group of the old tests exists on the worker path (the Task 1 table is the checklist; a dropped behaviour is on the list the user approved). Tasks 5 and 6.
2. **`run pipeline` and the worker share the lock:** the command takes it before queueing; a worker cannot start during the run; a lost lock mid-run stops the command with exit 1 and nothing is committed (as today). Task 3.
3. **A job left behind:** the command queues jobs; if it is interrupted (Ctrl-C, kill) the queued jobs of this run stay queued and a later worker run finishes them: the report/message says so and no document is lost or duplicated. Task 3.
4. **Dry run changes nothing:** no file, no row, no job, no gate record, no model call, no YouTube call. Task 4.
5. **The old loop is really gone** and nothing imports it; the shared helpers the handlers use survive. Task 7.

---

## File structure

| File | Responsibility |
| --- | --- |
| `src/catcher/modules/pipeline/report.py` (new) | `RunReport`/`ItemReport` (moved from run.py) and `report_for_job(session, job_id)`. |
| `src/catcher/modules/pipeline/preview.py` (new) | The read-only preview used by `--dry-run` (extracted from the old loop's dry-run branches). |
| `src/catcher/modules/worker/runner.py` (new) | `run_command(settings, options, ...)`: the in-process queue-and-drain used by `catcher run pipeline` and by the tests' helper. |
| `src/catcher/modules/worker/handlers_pipeline.py` | `pipeline.run` gets `refresh_facts`; `youtube.fetch` honours it. |
| `src/catcher/cli.py` | `run pipeline` calls the runner. |
| `src/catcher/modules/pipeline/run.py` | The loop and `RunOptions` are deleted; shared helpers stay (or move to `steps.py`/`outcome.py`). |
| `tests/support/run_on_worker.py` (new) | The test helper. |

---

### Task 1: Parity audit

**Files:**
- Create: `.superpowers/sdd/<plan>/parity-table.md` (a working document, not committed) and `docs/superpowers/plans/2026-10-06-idea-catcher-stage-b5b-parity-table.md` (committed: the table and the decisions)
- No source changes.

**Interfaces:**
- Produces: a table with one row per BEHAVIOUR pinned by the old loop's tests (group the 170 call sites by what they assert: naming, archive/output/failed/duplicates folders, report statuses and counts, messages, logs, exit codes, budget/rate-limit handling, snapshots/duplicates, requeue, retry_deferred, limits, only, YouTube waiting, artifacts, git commit/push, run lock) with columns: behaviour, the tests that pin it (file::name), how the worker path covers it today (handler/test) or `GAP`, the proposed action (`covered`, `add to worker`, `drop on purpose: reason`), and the effort (S/M/L).

- [ ] **Step 1: Read** `src/catcher/modules/pipeline/run.py` fully, the `run pipeline` command in `cli.py`, the worker handlers and their tests, and every test that calls `run_pipeline(` (`tests/unit/test_missing_paths.py`, `tests/unit/test_cli.py`, `tests/integration/db/test_worker_end_to_end.py`, `tests/integration/git/test_run.py`, `test_llm_traces_in_run.py`, `test_llm_cache.py`, `test_testdata_run.py`, `test_llm_saved_flag.py`).
- [ ] **Step 2: Write** the table and a short list of the decisions the user must make (every `drop on purpose` row and every `L` gap).
- [ ] **Step 3: Verify** the table is complete: the number of test functions that call `run_pipeline(` equals the number of rows' test references (a script that counts both is fine); no test is unaccounted for.
- [ ] **Step 4: Commit** `docs: the parity table for retiring the old pipeline loop`.
- **Replan point:** the controller reads the table with the user and adjusts Tasks 2 to 6 before they start.

### Task 2: Worker gaps and the report from the database

**Files:**
- Create: `src/catcher/modules/pipeline/report.py`
- Modify: `src/catcher/modules/pipeline/run.py` (the report classes move; re-exported while the loop exists), `src/catcher/modules/worker/handlers_pipeline.py` (`refresh_facts`, and any other gap Task 1 marks `add to worker`)
- Test: `tests/integration/db/test_run_report.py` (new), `tests/integration/db/test_handler_pipeline_run.py` and `test_handler_youtube_fetch.py` (extend)

**Interfaces:**
- Produces: `report_for_job(session: Session, job_id: uuid.UUID) -> RunReport` (items from `job_items` with `root_job_id == job_id`: status mapped to the old report statuses (`published`, `deferred`, `failed`, `duplicate`, `artifact`, `requeued`, `skipped` for the documents left in the inbox by `limit` (from the job result), `waiting` for items still waiting for YouTube), messages from `stage_reason`/`error`, `unreadable` and `not_found` from the job result); `pipeline.run` param `refresh_facts: bool` copied to the `youtube.fetch` job; its `Done` counts gain only what Task 1 shows is needed (update every test that compares the exact dict).
- Consumes: Task 1's table.

- [ ] **Step 1: Write the failing tests** named after the Task 1 rows for this task, at least: `test_report_for_a_run_counts_published_deferred_failed_and_duplicates`, `test_report_marks_the_documents_left_by_the_limit`, `test_report_lists_unreadable_files_and_names_not_found`, `test_refresh_facts_fetches_even_when_facts_are_saved`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** `report.py` and the handler changes.
- [ ] **Step 4: Run** `uv run pytest tests/integration/db -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: the run report is built from the database, and pipeline.run takes refresh_facts`.

### Task 3: The runner and the `run pipeline` command (strict review)

**Files:**
- Create: `src/catcher/modules/worker/runner.py`
- Modify: `src/catcher/cli.py` (`run_pipeline_cmd`)
- Test: `tests/integration/db/test_run_command.py` (new); the tests of `run pipeline` through `CliRunner` in `tests/integration/db/test_run_pipeline_cli.py` stay and pass

**Interfaces:**
- Produces: `run_command(settings: Settings, *, ideas: Path, docs: Path, params: dict[str, Any], push: bool, wait_youtube_s: float | None, services: Services | None = None, clock: Callable[[], datetime] = utc_now) -> RunOutcome` (`RunOutcome`: `report: RunReport`, `committed: dict[str, bool]`, `pushed: bool`, `left_queued: int`, `exit_code: int`); it takes `WorkerLock` first, queues `pipeline.run`, drains with the existing `Worker` (no new loop), queues `pipeline.publish` with `{"push": push}`, drains again, builds the report with `report_for_job`; the command prints `_print_items(report)` and `summary: {counts} committed=... pushed=...` exactly as today and returns `exit_code` (0, 1, 2 as today: 2 for a wrong path/database/lock problem, 1 for a failed document, a name not found or an unreadable file).
- Consumes: `Worker`, `WorkerLock`, `queue.enqueue`, `report_for_job` (Task 2).

- [ ] **Step 1: Write the failing tests:** `test_run_pipeline_publishes_the_inbox_and_commits_and_prints_the_summary`, `test_run_pipeline_options_become_job_params` (limit, file, requeue, retry_deferred, profile, refresh_llm, refresh_facts), `test_run_pipeline_exits_1_when_a_document_failed`, `test_run_pipeline_with_push_pushes_to_the_remote`, `test_run_pipeline_leaves_the_clips_that_wait_for_youtube_queued_and_says_so` (Review Focus 3), `test_wait_youtube_keeps_polling_until_the_fetch_is_due` (frozen clock), `test_a_worker_cannot_start_during_the_run` (Review Focus 2), `test_ctrl_c_leaves_the_queued_jobs_for_a_later_worker`, `test_the_lock_lost_mid_run_exits_1_and_commits_nothing`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** `runner.py` and rewire the command; `--dry-run` still calls the old path until Task 4.
- [ ] **Step 4: Run** `uv run pytest tests/integration/db -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: catcher run pipeline queues the run and drains it with the worker`.

### Task 4: The dry-run preview

**Files:**
- Create: `src/catcher/modules/pipeline/preview.py`
- Modify: `src/catcher/cli.py` (`--dry-run` calls the preview)
- Test: `tests/integration/db/test_preview.py` (new)

**Interfaces:**
- Produces: `preview(ideas: Path, docs: Path, params: dict[str, Any], services: Services) -> RunReport` (read-only: scans the inbox, classifies each document with the saved replies and saved facts, statuses `would_publish`, `would_call_llm`, `would_fetch`, `duplicate`, `artifact`, `failed` for an unreadable file; changes no file, writes no row, queues no job, records nothing in the gate, calls no model and no YouTube).

- [ ] **Step 1: Write the failing tests:** `test_the_preview_changes_nothing` (Review Focus 4: a byte hash of both repos, an empty `job_items`, no jobs, the `youtube` row unchanged, a raising backend and fetcher), `test_the_preview_says_what_a_run_would_do_per_document`, `test_the_preview_uses_saved_replies_and_saved_facts`, `test_the_preview_exit_codes_and_output_format_match_today`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** by extracting the old loop's dry-run branches; keep the same wording of the lines.
- [ ] **Step 4: Run** `uv run pytest tests/integration/db -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: --dry-run is a read-only preview of its own`.

### Task 5: Move the old loop's tests, part 1 (strict review)

**Files:**
- Create: `tests/support/run_on_worker.py` (`run_on_worker(repos, services, **options) -> RunReport`: does what the command does with the given services; options named like the old `RunOptions` fields)
- Modify: `tests/integration/git/test_run.py` (about 98 call sites) and `tests/integration/git/test_testdata_run.py`, `tests/integration/db/test_worker_end_to_end.py`; move the files to `tests/integration/db/` when they need `pg_engine`
- Test: the moved tests themselves; each Task 1 row for these files is checked off in the table

**Interfaces:**
- Consumes: Tasks 2-4. Produces: the helper and the migrated tests; every assertion keeps its meaning (a changed assertion needs the Task 1 table row that allows it).

- [ ] **Step 1: Write the helper and its own tests** (`tests/integration/db/test_run_on_worker.py`): same report shape and statuses as the old `run_pipeline`.
- [ ] **Step 2: Migrate** the call sites in batches of about 20 tests; after each batch run only those tests (`-k`), one session at a time.
- [ ] **Step 3: For each test that cannot pass unchanged,** stop and list it with the Task 1 row; the controller rules (change the assertion with the user's approved reason, add a worker behaviour, or drop the test on purpose).
- [ ] **Step 4: Run** `uv run pytest tests/integration -q`. Expected: PASS.
- [ ] **Step 5: Commit** per batch: `test: the old pipeline loop's tests run on the worker path (batch N)`.

### Task 6: Move the old loop's tests, part 2 (strict review)

**Files:**
- Modify: `tests/integration/git/test_llm_cache.py` (about 46 call sites), `test_llm_traces_in_run.py` (15), `test_llm_saved_flag.py` (4), `tests/unit/test_missing_paths.py` (4), `tests/unit/test_cli.py` (1)
- Test: the migrated tests

**Interfaces:**
- Consumes: Task 5's helper.

- [ ] **Step 1: Migrate** as in Task 5 (the unit tests that only check argument errors (`test_missing_paths.py`) keep running without Docker through the command's early exits: keep them Docker-free: they must still pass unchanged).
- [ ] **Step 2: Run** `uv run pytest tests/unit tests/component tests/integration -q`. Expected: PASS; the count of tests equals the count before minus the dropped ones listed in the table.
- [ ] **Step 3: Commit** `test: the saved-reply and trace tests run on the worker path`.

### Task 7: Delete the old loop (strict review)

**Files:**
- Modify: `src/catcher/modules/pipeline/run.py` (delete `run_pipeline`, `RunOptions`, the loop-only state and the lock-check plumbing; keep what the handlers import), `src/catcher/cli.py`, `docs/*`
- Test: `tests/unit/test_old_loop_is_gone.py` (new: no `run_pipeline` in `src`; the handlers still import their helpers)

**Interfaces:**
- Produces: a `run.py` that only holds shared helpers (or none: move them to `steps.py`/`outcome.py`); no import of `run_pipeline` anywhere.

- [ ] **Step 1: Write the failing test** `test_the_old_loop_is_gone` (scan `src` for `def run_pipeline`, `class RunOptions`, `RunLockLost` and `lock_check`).
- [ ] **Step 2: Run** it. Expected: FAIL.
- [ ] **Step 3: Delete** the code, fix imports, delete dead helpers (grep each for remaining users first).
- [ ] **Step 4: Run** `scripts/check` once. Expected: all checks pass.
- [ ] **Step 5: Docs:** `docs/idea-catcher-how-to-run.md` (the `run pipeline` section: what it does now, the options, `--dry-run` as a preview, `--wait-youtube`), `docs/idea-catcher-how-to-run-stage-b.md`, `docs/idea-catcher-pipeline.md` (the Stage A loop description becomes the worker path), `docs/idea-catcher-service-architecture.md` (B5b built with the date; remove the B5b open items; list what was dropped on purpose), `README.md`; verify the recipe for real on a throwaway Postgres (a random-port `docker run`, never `catcher-db`; a throwaway copy of the test repos; `OPENAI_API_KEY=""`, `FREELLMAPI_URL=http://127.0.0.1:1/v1`, `YOUTUBE_OFFLINE=1`): `run pipeline --limit 3`, `--dry-run`, `--requeue`, a run interrupted with Ctrl-C and finished by `catcher worker --once`.
- [ ] **Step 6: Commit** `refactor: the old pipeline loop is deleted, run pipeline is the worker path` and `docs: Stage B5b built`.
