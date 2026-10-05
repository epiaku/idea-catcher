# Stage B4b: Postgres Is the Only Truth for the Gate and the Run Lock Implementation Plan

**Status:** built 2026-10-04 (commits 3b34541 to the docs commit on `stage-b`); verified by hand on a throwaway database on 2026-10-05.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every call to YouTube, from the worker and from the Stage A commands, goes through the Postgres gate, and `run pipeline` takes the same Postgres lock as the worker, so the file gate, the `pipeline.lock` file and `CATCHER_STATE_DIR` are deleted.

**Direction (the user, 2026-10-04):** every command and every change is made with one end goal: the system runs on Postgres as the single truth, with no mode that works with or without it. When the database is not running the system does not run. This plan removes the file-held state that is left in the code paths (the gate file and the run lock); later steps (B5) move the rest (the LLM blocks, the item states, reconcile).

**Architecture:** `build_access(settings)` always builds a `PostgresGate` on `DATABASE_URL` (no fallback to a file). The commands `run pipeline` and `youtube facts` need the database: `run pipeline` takes the worker's Postgres advisory lock (`WorkerLock`) before it does anything, so it cannot run while a worker runs, and exits 2 when the database is unreachable; `youtube facts` exits 2 the same way. `run_pipeline()` the function holds no lock itself (the command does), so its tests stay simple. The unit and component tests, which run without Docker, use a small in-memory test double of the `Gate` protocol that lives in `tests/support`, built on the same shared rules.

**Tech Stack:** Python 3.12, sync SQLAlchemy 2 + psycopg 3, pytest, `scripts/check`.

**Spec:** the user's decision of 2026-10-04: "we will not build a system that works with or without Postgres, only with Postgres, so the truth should always be in Postgres", and `docs/idea-catcher-service-architecture.md` (Stage B4 and "Open items after B4", the "two gates on one machine" item this plan removes).

## Defaults I chose (change any before we start)

1. **`run pipeline` and `youtube facts` need the database**, per your direction. `run pipeline` (also with `--dry-run`) takes the Postgres advisory lock first: it exits 2 with a short message (no URL) when the database is unreachable and exits 2 with `another worker or run is already running` when the lock is taken (this also closes the open gap that a Stage A run and a worker could work on one checkout at the same time). `youtube facts` exits 2 when the gate is unavailable. `scan`, `reason` and `render` stay as they are: they read or write one document and any YouTube access in them goes through the Postgres gate (a clip defers with `YouTube gate unavailable` when the database is down).
2. **The file gate is deleted:** class `YoutubeGate`, its state file `youtube-gate.json`, the `.corrupt` handling, the file reader, and `catcher youtube gate --import-file` (you have no block open, so there is nothing left to import). `catcher youtube gate` (show the state) stays.
3. **`CATCHER_STATE_DIR` is deleted** with the file gate and `pipeline.lock` (the run lock is the Postgres advisory lock now). The shared gate rules stay: `PostgresGate` uses them. Leftover files in `~/.catcher/state` can be deleted by hand.
4. **Test double:** `tests/support/memory_gate.py` with `InMemoryGate`, which implements `Gate` with the shared rules and an injected clock, so unit and component tests (which run without Docker, in the pre-commit hook) never need Postgres. The Postgres gate keeps its own contract tests against a real Postgres.
5. **Nothing else changes in behaviour:** the worker, the claim skip, the 60 s and first-breaker-step deferrals, `catcher youtube gate`.

## Global Constraints

- ruff line length 110; pyright standard on `src`; `scripts/check` passes at the end of every task (once per task).
- Unit and component tests must not need Docker or Postgres (they run in the pre-commit hook and in `scripts/check --fast`).
- No live YouTube and no live LLM in any test; the whole suite runs with the network blocked.
- Never modify `tests/data` or `tmp/ic`; the user's container `catcher-db` runs on port 5432 and must not be stopped or touched; run ONE pytest session at a time (earlier runs stalled when two ran together).
- Commit locally on `stage-b`; do not push.

## Review Focus

1. **`run pipeline` with the database down** exits 2 before it touches any file (no partial run); with a worker holding the lock it exits 2 with a clear message and changes nothing. Task 2.
2. **`catcher youtube facts` with the database down** exits 2 with a short message and prints no database URL. Task 2.
3. **No fetch without the gate:** nothing in `src` can reach the YouTube fetcher without going through a `Gate`. Task 2 (grep test or a test that builds the real services and asserts the gate type).
4. **The deleted file gate leaves no coverage hole:** each behaviour the old file-gate tests pinned is pinned by `tests/unit/test_gate_rules.py`, the contract suite or the Postgres tests. Task 3.
5. **The pre-commit tests run without Docker** after the change. Task 1 and 3.
6. **No file-held state left in these paths:** `grep -rn "state_dir\|youtube-gate\|pipeline.lock" src` finds nothing. Task 3.

---

## File structure

| File | Responsibility |
| --- | --- |
| `tests/support/memory_gate.py` (new) | `InMemoryGate`: the `Gate` protocol on the shared rules, in memory, injected clock and rng. |
| `src/catcher/modules/youtube/access.py` | `build_access(settings, gate=None, ...)` builds a `PostgresGate` when no gate is given. |
| `src/catcher/modules/youtube/gate.py` | Keeps `Gate`, `Wait`, `GateUnavailable`, `is_block_error`, `MAX_BLOCK_HOURS`; `YoutubeGate` is deleted. |
| `src/catcher/cli.py` | `run pipeline` takes the Postgres lock and exits 2 without the database; `youtube facts` exits 2 when the gate is unavailable; `--import-file` and its helpers are deleted. |
| `src/catcher/modules/pipeline/run.py` | The `pipeline.lock` file lock is removed (the command holds the Postgres lock). |
| `src/catcher/core/config.py` | `catcher_state_dir` is removed. |
| tests | the tests that built a `YoutubeGate(...)` use `InMemoryGate`; `tests/unit/test_youtube_gate.py` (file gate) is deleted. |

---

### Task 1: The in-memory gate for tests

**Files:**
- Create: `tests/support/memory_gate.py`
- Test: `tests/unit/test_memory_gate.py` (new)
- Modify: every test that builds a `YoutubeGate(...)` as a test double: `tests/integration/git/test_run.py` (~702-717), `tests/integration/git/test_testdata_run.py` (~42), `tests/component/test_process_steps.py` (~91-101), `tests/component/test_youtube_access.py` (~54), `tests/integration/db/test_worker_end_to_end.py` (~101), `tests/integration/db/test_pg_gate.py` (~47: the contract suite is parametrized over the FILE gate and the Postgres gate: it becomes InMemoryGate and the Postgres gate), `tests/conftest.py` if it builds one (read each). `tests/unit/test_youtube_gate.py` (the file gate's own tests) stays until Task 3 deletes it.

**Interfaces:**
- Produces: `class InMemoryGate` with `__init__(self, *, min_gap_s: float = 120.0, jitter_s: float = 300.0, block_hours: float = 6.0, clock: Callable[[], float] = time.time, rng: Callable[[], float] = random.random)` and the four `Gate` methods (`peek`, `reserve`, `record_success(started_at=None)`, `record_block(started_at=None) -> float`) with the same return types and semantics as `PostgresGate`, built only from `gate_rules`; a `state` attribute (`GateState`) tests can read and set.

- [ ] **Step 1: Write the failing tests** `tests/unit/test_memory_gate.py`: `test_a_new_gate_is_open`, `test_reserve_then_the_gap_holds_a_second_call`, `test_a_block_opens_the_breaker_and_doubles`, `test_a_success_closes_the_breaker`, `test_a_newer_block_survives_an_older_success`, `test_state_can_be_set_by_a_test`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_memory_gate.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `InMemoryGate`, then replace every `YoutubeGate(...)` used as a test double in the files listed above by `InMemoryGate(...)` with the same arguments (tmp state dirs are no longer needed there). Existing assertions stay unchanged.
- [ ] **Step 4: Run** `uv run pytest tests/unit tests/component tests/integration/git -q`. Expected: PASS, and `git diff --stat -- tests` shows only the new files and the swapped constructors.
- [ ] **Step 5: Commit** `test: an in-memory gate for the tests that do not need Postgres`.

### Task 2: Production uses only the Postgres gate

**Files:**
- Modify: `src/catcher/modules/youtube/access.py` (`build_access`), `src/catcher/cli.py` (`youtube facts`, `run pipeline`), `src/catcher/modules/pipeline/run.py` (remove the `pipeline.lock` file lock at ~230), `src/catcher/modules/pipeline/process.py` (`default_services` builds its access through `build_access`: read it)
- Test: `tests/component/test_youtube_access.py` (extend), `tests/integration/db/test_youtube_gate_cli.py` (extend), `tests/integration/db/test_run_pipeline_cli.py` (new), `tests/integration/git/test_run.py` (the tests of the file lock at ~890-903 move to the new db test file)

**Interfaces:**
- Consumes: `PostgresGate(engine, *, name, min_gap_s, jitter_s, block_hours, clock, rng)`, `make_worker_engine(settings.database_url)` from `catcher.core.db`.
- Produces: `build_access(settings: Settings, gate: Gate | None = None, clock: Callable[[], float] = time.time) -> YoutubeAccess` where a missing `gate` builds `PostgresGate(make_worker_engine(settings.database_url), min_gap_s=settings.youtube_min_gap_s, jitter_s=settings.youtube_gap_jitter_s, block_hours=settings.youtube_block_hours, clock=clock)`. The engine is created lazily (no connection until the first gate call) so building services never fails when the database is down.

- [ ] **Step 1: Write the failing tests:** component `test_build_access_without_a_gate_builds_a_postgres_gate` (`isinstance(access.gate, PostgresGate)`, no connection made while building, DATABASE_URL pointing at a closed port); `test_no_file_gate_is_left` (importing `catcher.modules.youtube.gate` has no `YoutubeGate`); db CLI `test_run_pipeline_exits_2_when_the_database_is_down_and_changes_nothing` (Review Focus 1: DATABASE_URL on a closed port; no file moved; no URL in the output), `test_run_pipeline_exits_2_while_a_worker_holds_the_lock` (a `WorkerLock` held in the test; nothing moved), `test_run_pipeline_takes_and_releases_the_lock` (a second `run pipeline` works after the first), `test_dry_run_also_needs_the_database`; component `test_a_clip_is_deferred_with_gate_unavailable_when_reason_runs_without_a_database` (services whose gate is a `PostgresGate` on a refused connection: the fake fetcher is never called); db CLI `test_youtube_facts_exits_2_when_the_gate_is_unavailable` (Review Focus 2: DATABASE_URL on a closed port; output has no URL) and `test_youtube_facts_goes_through_the_postgres_gate` (a block in the row defers it without a fetch).
- [ ] **Step 2: Run** the new tests. Expected: FAIL.
- [ ] **Step 3: Implement** the change in `build_access`; make `youtube facts` turn `GateUnavailable` into a short message and exit 2; in the `run pipeline` command take `WorkerLock(engine)` (the same advisory lock the worker takes: read worker/guard.py and how `catcher worker` reports `WorkerAlreadyRunning`) before any file is touched, with the same short exit-2 messages as `worker`; remove the `pipeline.lock` lock from `run_pipeline()`; the integration tests that called `run_pipeline()` directly keep working unchanged.
- [ ] **Step 4: Run** `uv run pytest tests/unit tests/component tests/integration -q` (one session). Expected: PASS.
- [ ] **Step 5: Commit** `feat: YouTube and the run lock go through Postgres`.

### Task 3: Delete the file gate

**Files:**
- Modify: `src/catcher/modules/youtube/gate.py` (delete `YoutubeGate`, its file reading, `_fail_closed`, the `.corrupt` handling, `file_lock` use, `snapshot`; keep the rest), `src/catcher/cli.py` (delete `--import-file`, `_import_state_file`, `_gate_clock` if unused, the `YoutubeGate` import), `src/catcher/modules/youtube/pg_gate.py` (delete `import_state` only if nothing else uses it: read), `src/catcher/core/config.py` (delete `catcher_state_dir`), `tests/conftest.py` (the `_private_state_dir` fixture)
- Delete: `tests/unit/test_youtube_gate.py` (the file gate's tests) after checking each behaviour it pinned is covered elsewhere (list the mapping in your report)
- Test: `tests/unit/test_youtube_settings.py`, `tests/integration/db/test_youtube_gate_cli.py` (remove the import tests)

**Interfaces:**
- Produces: no `YoutubeGate`, no `youtube-gate.json`, no `--import-file`, no `pipeline.lock`, no `catcher_state_dir` setting and no `CATCHER_STATE_DIR` (an old value in a user's `.env` is ignored, not an error: check how `Settings` treats unknown env names and keep that behaviour).

- [ ] **Step 1: Write the failing tests:** `test_the_file_gate_is_gone` (no `YoutubeGate` in `catcher.modules.youtube.gate`; `catcher youtube gate --help` has no `--import-file`); `test_no_file_state_is_left` (a grep-style test over `src`: no `state_dir`, `youtube-gate` or `pipeline.lock`; Review Focus 6).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Delete** the code and tests listed; fix every import that breaks. In the report map each deleted file-gate test to the test that now covers its behaviour (rules in `test_gate_rules.py`, contract tests in `test_pg_gate.py`, `test_memory_gate.py`); if a behaviour is not covered, add the test where it belongs.
- [ ] **Step 4: Run** `scripts/check` once. Expected: all checks pass (also `scripts/check --fast` passes without Docker).
- [ ] **Step 5: Commit** `refactor: delete the file gate, Postgres is the only gate`.

### Task 4: Docs

**Files:**
- Modify: `docs/idea-catcher-how-to-run.md` (`#youtube-gap`: one gate, in Postgres; Stage A commands need the database for YouTube; `catcher youtube gate`), `docs/idea-catcher-how-to-run-stage-b.md` (remove `--import-file`; the old file can be deleted: `rm ~/.catcher/state/youtube-gate.json`), `docs/idea-catcher-configuration.md` (remove `CATCHER_STATE_DIR`; the YOUTUBE_* settings), `docs/idea-catcher-service-architecture.md` (a dated decision: Postgres is the only truth, no file mode; remove the "two gates on one machine" and "import exit code" open items and the "file-backed fix" mentions; B4b built), `docs/idea-catcher-diagram-processing-loop.md` and `docs/idea-catcher-youtube-bans-and-queue-options.md` if they describe the file gate, `.env.example` (remove CATCHER_STATE_DIR), `README.md` (the gate rows), this plan (a status line).

- [ ] **Step 1: Verify for real** on your own throwaway Postgres (a random-port `docker run`, never `catcher-db`): `run pipeline` with the database stopped exits 2 and moves nothing; with a worker lock held it exits 2; `youtube facts` exits 2; with the database up and a block in the row `youtube facts` defers. No live YouTube (a fake 429 through `PostgresGate.record_block` in a snippet).
- [ ] **Step 2: Write** the doc changes, short, plain words.
- [ ] **Step 3: Run** `scripts/check` once. Expected: all checks pass.
- [ ] **Step 4: Commit** `docs: one YouTube gate, only in Postgres`.
