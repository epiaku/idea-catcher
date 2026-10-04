# Stage B4: The YouTube Gate in Postgres Implementation Plan

**Status:** built 2026-10-04 (Tasks 1-6, docs in Task 7); the final whole-branch review and its fix wave are done (2026-10-04).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The worker keeps the YouTube gap and breaker in the Postgres `resources` table instead of a file, so two workers (or a worker in a container) can never break the gap, and waiting fetch jobs do not spin through the queue.

**Architecture:** The rules of the gate (gap plus jitter, breaker 6/12/24 hours, the "newer block wins" rule, fail closed) move into pure functions that the file gate (Stage A, no database) and a new `PostgresGate` (worker) share, so they cannot drift. `PostgresGate` implements the existing `Gate` protocol on the `resources` row `youtube`, with `SELECT ... FOR UPDATE` inside a short transaction. The queue `claim` skips jobs whose `resource` is closed, so a waiting `youtube.fetch` job is not claimed and deferred over and over.

**Tech Stack:** Python 3.12, sync SQLAlchemy 2 + psycopg 3, Alembic, pytest with a real Postgres 17 (testcontainers), `scripts/check`.

**Spec:** `docs/idea-catcher-service-architecture.md` (step B4, Stage B decisions 8 and 9, "Open items after B3"), the docstring of `src/catcher/modules/youtube/gate.py` (the rules to keep), `docs/idea-catcher-how-to-run.md#youtube-gap` (what the user sees).

## Defaults I chose (change any of them before we start)

1. **Two gates, one set of rules.** The file gate stays for the Stage A CLI, because Stage A needs no database. The worker uses the Postgres gate. Both implement the existing `Gate` protocol (`peek`, `reserve`, `record_success`, `record_block`), so `YoutubeAccess` does not change. The rules live in one pure module both use.
2. **One row, named `youtube`**, in `resources`. Migration `0004` inserts it open. A row that is missing later or has damaged values means **closed** (decision 9): the gate closes for `YOUTUBE_BLOCK_HOURS`, rewrites the row, and logs an error, like the file gate does with a damaged file.
3. **The handler still reserves right before the fetch**, in its own short transaction. The architecture row said "the claim reserves the slot"; I changed it: the claim only **skips** fetch jobs while the gate is closed (it does not reserve), and the row lock in `reserve` is what guarantees the gap. Same safety, simpler claim. It stays true that a 429 pushes the job's `run_after` past the block without counting an attempt (B3 already does that through `Defer`).
4. **A database error means closed.** If the gate cannot read or write its row, the fetch is **not** made; the job is deferred for 60 seconds and the error is logged. A database hiccup must never open the gate and must never fail a document for good.
5. **Time.** The gate keeps the protocol's epoch-seconds `clock`; the worker passes `lambda: ctx.clock().timestamp()` so tests freeze it with the existing clock. Stored as `timestamptz`, no SQL `now()`.
6. **No automatic import of the old state file.** A block that is open in `youtube-gate.json` is the expensive thing to lose, so `catcher youtube gate --import-file` copies it into the row once, on purpose. `catcher youtube gate` shows the row.
7. **Not in B4:** the LLM resources (`openai`, `freellmapi`, `git`) and a Postgres version of the LLM blocks. That is B5.
8. **One worker is still the rule** (the advisory lock). B4 makes the gate safe for more than one; it does not allow them.

## Global Constraints

- ruff line length 110; pyright standard on `src`; `scripts/check` passes at the end of every task (run it once per task, not in a loop).
- Sync SQLAlchemy 2 and psycopg 3; every queue and gate function takes its time from Python (an injected `clock` or an aware `now`), never SQL `now()` (decision 8).
- No live YouTube and no live LLM in any test; the fetcher in tests is the counting fake; the whole suite runs with the network blocked.
- Never modify `tests/data` or `tmp/ic`. Stage A tests stay unchanged and green (the file gate keeps its behaviour and its tests).
- The gate rules to keep exactly: gap `YOUTUBE_MIN_GAP_S` (120) plus random jitter up to `YOUTUBE_GAP_JITTER_S` (300) between the **start** of two fetches; breaker `YOUTUBE_BLOCK_HOURS` (6), then 12, then 24 hours at most, until a fetch works; a value more than 24 hours in the future is damage, not a block; a block recorded after a fetch started is newer news and a success of that older fetch does not close it.
- Commit locally on `stage-b`; do not push.

## Review Focus

1. **Two workers reserve at the same moment** (two engines, two threads): exactly one gets "go ahead", the other gets the wait. Pinned in Task 3.
2. **The row is missing or has damaged values** (deleted by hand, a `blocked_until` in the year 2100): the gate is closed, the row is rewritten, an error is logged, and it never lets a fetch through. Task 3.
3. **The database is down or the lock times out during a fetch decision:** no fetch, the job is deferred 60 s, the item stays `waiting_youtube`. Task 4.
4. **Timestamps:** a value stored as `timestamptz` and read back as an epoch float must not make `now < next_allowed_at` flip at the microsecond (rounding), and an aware time with another time zone must not shift the gap. Task 3.
5. **Waiting fetch jobs while the gate is closed:** the worker is idle (no claim, no `job_events` flood, no attempt counted) and other job types still run; when the slot opens the oldest fetch job is claimed first. Task 5.
6. **A 429 during one fetch while another fetch was reserved earlier:** one block, not two; the older success does not close the newer block. Task 3 (contract tests).

---

## File structure

| File | Responsibility |
| --- | --- |
| `src/catcher/modules/youtube/gate_rules.py` (new) | Pure rules: `GateState`, `wait_for`, `after_reserve`, `after_success`, `after_block`, `clamp`, `closed_state`. No I/O. |
| `src/catcher/modules/youtube/gate.py` | `YoutubeGate` (file) now calls the rules; keeps `Gate`, `Wait`, `is_block_error`. Behaviour and tests unchanged. |
| `src/catcher/modules/youtube/pg_gate.py` (new) | `PostgresGate(engine, *, min_gap_s, jitter_s, block_hours, clock, rng)`, `GateUnavailable`. |
| `migrations/versions/0004_youtube_resource.py` (new) | Seeds the open `youtube` row; downgrade removes it. |
| `src/catcher/modules/youtube/access.py` | `build_access(settings, gate=None)`; `GateUnavailable` becomes `FactsDeferred`. |
| `src/catcher/modules/worker/app.py` | `build_context` passes a `PostgresGate`. |
| `src/catcher/modules/queue/queue.py` | `claim` skips jobs whose `resource` is closed. |
| `src/catcher/modules/worker/handlers_pipeline.py` | `youtube.fetch` jobs are enqueued with `resource="youtube"`. |
| `src/catcher/cli.py` | `catcher youtube gate [--import-file]`. |
| `tests/support/worker_harness.py` | The harness gate becomes a `PostgresGate`. |

---

### Task 1: Shared gate rules

**Files:**
- Create: `src/catcher/modules/youtube/gate_rules.py`
- Modify: `src/catcher/modules/youtube/gate.py`
- Test: `tests/unit/test_gate_rules.py` (new); the existing gate tests stay unchanged.

**Interfaces:**
- Produces: `@dataclass(frozen=True) class GateState: next_allowed_at: float; blocked_until: float; blocked_at: float; streak: int` (all 0 = open and never blocked); `wait_for(state: GateState, now: float) -> Wait | None` (blocked first, then the gap); `after_reserve(state, now, *, min_gap_s, jitter_s, rng) -> GateState` (sets `next_allowed_at = now + min_gap_s + rng() * jitter_s`); `after_success(state, started_at: float | None) -> GateState` (clears `streak` and `blocked_until`, except when `state.blocked_at > started_at`); `after_block(state, now, started_at, *, block_hours) -> GateState` (streak + 1, hours = `min(block_hours * 2 ** (streak - 1), max(24, block_hours))`, and unchanged when a block newer than `started_at` is still running); `clamp(state, now) -> GateState` (values beyond `now + 24 h` are cut to it; negative or non-finite raise `ValueError`); `closed_state(now, block_hours) -> GateState` (the fail-closed state: blocked for `max(block_hours, 1)` hours, streak 1).
- Consumes: `Wait` from `gate.py` (move `Wait` into `gate_rules.py` if that avoids a cycle, and re-export it from `gate.py`).

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_gate_rules.py`: `test_an_open_state_allows_a_call`; `test_reserve_sets_the_gap_with_jitter` (rng 0.5, gap 120, jitter 300 -> `now + 270`); `test_a_closed_gap_returns_the_gap_wait_and_a_block_wins_over_it`; `test_blocks_double_up_to_24_hours` (6, 12, 24, 24); `test_a_block_newer_than_the_fetch_is_not_doubled` and `test_a_success_of_an_older_fetch_does_not_close_a_newer_block`; `test_a_value_beyond_24_hours_is_clamped`; `test_a_negative_or_nan_value_is_rejected`; `test_closed_state_blocks_for_at_least_one_hour`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_gate_rules.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `gate_rules.py`, then make `YoutubeGate._read/_wait/reserve/record_success/record_block` call the rules (keep the file reading, the corrupt-file rename and the lock in `gate.py`). One approach: `_read` builds a `GateState`, the methods apply a rule function and `_write` the result.
- [ ] **Step 4: Run** `uv run pytest tests/unit/test_gate_rules.py tests/unit tests/component -q`. Expected: PASS, and no Stage A gate test edited (`git diff --stat -- tests` shows only the new file).
- [ ] **Step 5: Commit** `refactor: the gate rules in one pure module`.

### Task 2: The `youtube` resource row

**Files:**
- Create: `migrations/versions/0004_youtube_resource.py`
- Test: `tests/integration/db/test_schema.py` (extend the existing migration tests).

**Interfaces:**
- Produces: after `upgrade` the table `resources` holds the row `name='youtube'` with `next_allowed_at`, `blocked_until`, `blocked_at` NULL, `streak 0`, `concurrency 1`, `updated_at` set by the migration to the migration's own timestamp (a fixed Python value in the migration file is fine; no SQL `now()`); `downgrade` deletes that row.

- [ ] **Step 1: Write the failing test** `test_upgrade_seeds_the_open_youtube_row_and_downgrade_removes_it` (revision chain `0003 -> 0004`; `upgrade head` from empty gives the row; `downgrade -1` removes it; `downgrade base` still works).
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_schema.py -q`. Expected: FAIL.
- [ ] **Step 3: Write the migration** (revision id `0004`, down revision `0003`; insert with `ON CONFLICT DO NOTHING`).
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat: migration 0004 seeds the youtube resource row`.

### Task 3: `PostgresGate`

**Files:**
- Create: `src/catcher/modules/youtube/pg_gate.py`
- Test: `tests/integration/db/test_pg_gate.py` (new)

**Interfaces:**
- Consumes: Task 1 rules; Task 2 row; `session_scope(engine)` from `catcher.core.db`; `Resource` from `catcher.modules.queue.models`.
- Produces: `class GateUnavailable(RuntimeError)`; `class PostgresGate` with `__init__(self, engine: Engine, *, name: str = "youtube", min_gap_s: float = 120.0, jitter_s: float = 300.0, block_hours: float = 6.0, clock: Callable[[], float] = time.time, rng: Callable[[], float] = random.random)` and the four `Gate` methods with the same return types as `YoutubeGate`. Each method opens its **own** session and transaction, takes the row with `SELECT ... FOR UPDATE`, applies a rule, writes, commits. A missing row is inserted closed; damaged values (the rules raise) rewrite the row with `closed_state`; both log an error. Any `SQLAlchemyError` is raised as `GateUnavailable` (chained).

- [ ] **Step 1: Write the failing tests** in `tests/integration/db/test_pg_gate.py`. A contract suite parametrized over the file gate and the Postgres gate with one frozen clock, checking the same behaviour for both: `test_peek_changes_nothing`, `test_reserve_then_a_second_reserve_waits_for_the_gap`, `test_the_gap_has_jitter`, `test_a_block_opens_the_breaker_and_doubles`, `test_a_success_closes_the_breaker`, `test_a_newer_block_survives_an_older_success` (Review Focus 6). Postgres-only: `test_two_engines_reserving_together_get_one_go_ahead` (two engines, two daemon threads, a Barrier, 20 rounds; exactly one `None` per round) (Review Focus 1); `test_a_missing_row_closes_the_gate_and_is_rewritten` and `test_damaged_values_close_the_gate` (negative streak and a `blocked_until` in 2100 through raw SQL) (Review Focus 2); `test_microsecond_rounding_does_not_flip_the_gap` and `test_the_time_zone_of_the_session_does_not_shift_the_gap` (`SET TIME ZONE` on the connection) (Review Focus 4); `test_a_database_error_raises_gate_unavailable_and_changes_nothing` (dispose the engine pointing at a closed port or use a failing connection) (Review Focus 3).
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_pg_gate.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `PostgresGate`; convert between epoch floats and aware UTC datetimes at the edge (`datetime.fromtimestamp(x, UTC)`, `.timestamp()`), round to microseconds on write, and compare as floats after reading.
- [ ] **Step 4: Run** the same command three times. Expected: PASS, stable.
- [ ] **Step 5: Commit** `feat: the YouTube gate in Postgres`.

### Task 4: The worker uses the Postgres gate

**Files:**
- Modify: `src/catcher/modules/youtube/access.py`, `src/catcher/modules/worker/app.py`, `src/catcher/modules/pipeline/process.py` (`default_services` takes an optional gate), `tests/support/worker_harness.py`
- Test: `tests/integration/db/test_handler_youtube_fetch.py` (extend), `tests/component/test_youtube_access.py` (extend)

**Interfaces:**
- Consumes: `PostgresGate`, `GateUnavailable`.
- Produces: `build_access(settings: Settings, gate: Gate | None = None) -> YoutubeAccess` (a given gate replaces the file gate); `default_services(settings, *, gate: Gate | None = None)`; `build_context` creates `PostgresGate(engine, min_gap_s=settings.youtube_min_gap_s, jitter_s=settings.youtube_gap_jitter_s, block_hours=settings.youtube_block_hours, clock=lambda: clock().timestamp())` on the same engine; `YoutubeAccess` turns `GateUnavailable` from `peek` or `reserve` into `FactsDeferred("YouTube gate unavailable ...", until=now + 60)` (constant `GATE_RETRY_S = 60`).

- [ ] **Step 1: Write the failing tests:** component `test_a_gate_that_is_unavailable_defers_and_never_fetches` (fake gate raising `GateUnavailable`: `FactsDeferred.until` is now + 60, the fetcher is not called); `test_build_access_uses_a_given_gate`; db `test_a_database_error_in_the_gate_defers_the_fetch_job_by_60_seconds` (job `queued`, `run_after` +60 s, attempts unchanged, item still `waiting_youtube`, `fetch_calls == []`) (Review Focus 3); `test_the_worker_context_gate_is_a_postgres_gate_sharing_the_frozen_clock`. Change the harness to build its `YoutubeAccess` over a `PostgresGate` on `pg_engine` (same gap, jitter 0) and keep every existing worker, handler, end-to-end and crash test green without editing their assertions.
- [ ] **Step 2: Run** the new tests. Expected: FAIL.
- [ ] **Step 3: Implement** the changes above.
- [ ] **Step 4: Run** `uv run pytest tests/component tests/integration/db -q`. Expected: PASS (the Stage A tests and the file gate untouched).
- [ ] **Step 5: Commit** `feat: the worker keeps the YouTube gate in Postgres`.

### Task 5: The claim skips closed resources

**Files:**
- Modify: `src/catcher/modules/queue/queue.py`, `src/catcher/modules/worker/handlers_pipeline.py` (where `youtube.fetch` is enqueued)
- Test: `tests/integration/db/test_queue.py` (extend), `tests/integration/db/test_worker_loop.py` (extend)

**Interfaces:**
- Consumes: the `resources` row, `Job.resource`.
- Produces: `claim(...)` additionally ignores a queued job whose `resource` names a row with `blocked_until > now` or `next_allowed_at > now` (a job with `resource IS NULL`, or whose resource has no row, is not affected; the `now` is the claim's own aware `now`). Every `youtube.fetch` job is enqueued with `resource="youtube"`.

- [ ] **Step 1: Write the failing tests:** `test_claim_skips_a_job_whose_resource_is_blocked`; `test_claim_skips_a_job_whose_resource_is_in_its_gap`; `test_claim_takes_the_job_again_when_the_slot_is_open` (frozen clock past `next_allowed_at`); `test_a_job_without_a_resource_is_unaffected`; `test_other_job_types_still_run_while_fetch_jobs_wait`; `test_the_oldest_fetch_job_is_claimed_first_when_the_slot_opens`; worker-loop `test_a_worker_with_only_waiting_fetch_jobs_is_idle_and_counts_no_attempts` (run_once returns None, no `job_events` rows added, attempts 0) (Review Focus 5); `test_youtube_fetch_jobs_carry_the_youtube_resource`.
- [ ] **Step 2: Run** the new tests. Expected: FAIL.
- [ ] **Step 3: Implement** with a `NOT EXISTS` (or an outer join) on `resources` inside the claim statement; keep `FOR UPDATE SKIP LOCKED` and the order `priority DESC, run_after, created_at`.
- [ ] **Step 4: Run** `uv run pytest tests/integration/db -q`. Expected: PASS, including the two-worker claim tests unchanged.
- [ ] **Step 5: Commit** `feat: the claim skips jobs whose resource is closed`.

### Task 6: `catcher youtube gate`

**Files:**
- Modify: `src/catcher/cli.py`, `src/catcher/modules/youtube/pg_gate.py` (a `snapshot()` read and an `import_state(state)` write, both inside the lock)
- Test: `tests/integration/db/test_youtube_gate_cli.py` (new)

**Interfaces:**
- Produces: `catcher youtube gate` prints, for the database named by `DATABASE_URL`: `youtube: open` or `youtube: next call allowed at <time>` or `youtube: blocked until <time> (block <streak>)`, from `Wait.message`; `catcher youtube gate --import-file` reads `youtube-gate.json` from `CATCHER_STATE_DIR` through the file gate's own reader (damage rules included) and writes it into the row; it refuses (exit 2) when the row already holds a block that is longer than the file's (never shortens a block) and says what it did. Exit 2 with the usual short message when the database cannot be reached.

- [ ] **Step 1: Write the failing tests** with `CliRunner` and a migrated fresh database: `test_gate_shows_an_open_gate`, `test_gate_shows_a_block_until_a_time`, `test_import_file_copies_a_block_into_the_row`, `test_import_file_never_shortens_a_longer_block`, `test_import_file_without_a_file_says_so_and_changes_nothing`, `test_a_damaged_state_file_imports_as_closed`.
- [ ] **Step 2: Run** the new file. Expected: FAIL.
- [ ] **Step 3: Implement** the command in the existing `youtube` command group of `cli.py`.
- [ ] **Step 4: Run** `uv run pytest tests/integration/db/test_youtube_gate_cli.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: catcher youtube gate shows and imports the gate state`.

### Task 7: Docs

**Files:**
- Modify: `docs/idea-catcher-how-to-run.md` (the YouTube gap section), `docs/idea-catcher-how-to-run-stage-b.md`, `docs/idea-catcher-configuration.md` (`CATCHER_STATE_DIR` is for the Stage A CLI; the worker keeps the gate in Postgres), `docs/idea-catcher-service-architecture.md` (B4 built with the date, what changed from the plan: the handler reserves and the claim only skips; the open items it settles; update the "M5 re-defer" and "B5 reconcile" notes), `README.md` if it mentions the gate file, this plan (status line).

- [ ] **Step 1: Verify the recipe for real** on your own throwaway Postgres (`docker compose` is in use by the user: use a random-port `docker run`, remove it afterwards): `db upgrade`, `catcher youtube gate` (open), a fake fetcher through a Python snippet that records a 429 (`PostgresGate.record_block`), `catcher youtube gate` (blocked), and `--import-file` on a throwaway state dir. No YouTube call.
- [ ] **Step 2: Write** the doc changes, short; plain words.
- [ ] **Step 3: Run** `scripts/check`. Expected: all checks pass.
- [ ] **Step 4: Commit** `docs: Stage B4 built`.
