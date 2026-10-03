# Idea Catcher Stage B3: the worker and the job handlers

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `catcher worker` that claims jobs from the Postgres queue and runs them through handlers (`pipeline.run`, `youtube.fetch`, `llm.reason`, `pipeline.publish`), so that jobs added by hand process an inbox into the same pages as Stage A, and an LLM failure never causes a YouTube call. The worker reuses saved YouTube facts and saved LLM replies, so the committed real-run test data processes with no model and no YouTube call.

**Architecture:** B0-B2 gave us the pipeline pieces and a correct queue. B3 adds a synchronous worker loop (claim and commit, run the handler with no open transaction, a heartbeat thread, finish in a fresh session, a reaper on a timer) and four handlers that call the Stage A functions. The database row is written before any file moves (decision 5), a clip waiting for YouTube is a staged item with its own job, and Postgres plus the Git history can rebuild what a crash leaves behind.

**Tech Stack:** Python 3.12, uv, SQLAlchemy 2 (sync) + psycopg 3, Typer, threading, pytest with the real-Postgres fixtures of B1, real Git repos (bare + clones) as in `tests/integration/git`.

**Updated 2026-10-03** after the saved-LLM-reply feature (a run reads a good saved reply from `llm/` before calling the model; `--refresh-llm`; the frozen real run in `tests/data/idea-bucket/{facts,llm}` and `tests/data/expected/`; the idea-type priority rule). Tasks 6, 8, 9, 11, 12, 13 and 14 changed accordingly.

**Spec:** `docs/idea-catcher-service-architecture.md`, section "Stage B: Postgres + the queue": the **Stage B decisions (2026-10-02)** (1-14), the B3 step row, and the **"Minors carried into B3"** list; `docs/idea-catcher-youtube-bans-and-queue-options.md` for the gate. Out of scope: the gate in Postgres (B4), state/metrics/mirror renames/reconcile (B5), the scheduler (B6), the worker container (B7), the backfill (B8), the API.

## Decisions taken for B3 (defaults; veto any before execution)

1. **One worker process, one job at a time** (the single Git writer). A Postgres advisory lock held for the worker's lifetime refuses a second worker. More workers come with B4's `git` resource.
2. **Transaction rules:** claim in its own committed session; the handler runs with **no open DB transaction** (it opens short sessions); a heartbeat thread with its own session every `lease_s / 3`; finish (`complete/defer/fail`) in a fresh session; `reap` at worker start and every 60 s, errors logged, never raised. Worker sessions set `lock_timeout` (10 s).
3. **Job vs item outcomes:** an LLM or facts problem is an *item* state, not a job failure: the job ends `succeeded` and the item is `deferred` (or `failed`), as in Stage A (decision 3). A job is `failed` only for an unexpected exception or an unknown job type. A closed YouTube gate defers the *job* with `run_after` (no attempt counted).
4. **YouTube clip flow:** `pipeline.run` stages it `waiting_youtube` and enqueues `youtube.fetch`; the fetch handler saves the facts and enqueues `llm.reason` (`waiting_llm`). `llm.reason` reads **saved facts only**; if they are missing it re-enqueues the fetch.
5. **Publish is manual in B3:** `pipeline.run` does not publish. `pipeline.publish` (pull, commit the managed folders, push right after) is added with `catcher jobs add`. The schedule comes in B6.
6. **Dry runs never go through the queue:** `catcher jobs add` refuses `dry_run`.
7. **Frontmatter stays as in Stage A** (`stage: analyzed`, `deferred_at`, `deferred_reason`): the rename to `stage_reason`/`stage_since` is B5.
8. **Retry of deferred items:** `pipeline.run` takes `retry_deferred` (the same as the CLI flag); the scheduled automatic version is B6.
9. **Saved LLM replies (new):** `llm.reason` goes through `ask_llm` with `llm_dir = ideas/"llm"`, so it reads a good saved reply before calling the model and records a trace after a real call, exactly like `catcher run pipeline`. A rerun after a crash therefore does not pay twice. `pipeline.run` takes `refresh_llm` (the CLI's `--refresh-llm`) and copies it, like `profile`, into the params of every `llm.reason` job it enqueues; `retry_deferred`/`requeue` do not imply it. A reply that produced an invalid page is marked `invalid_page` by the handler too (the marking in `run.py` moves into a function both use). The trace files and `facts/` belong to the commit set of `pipeline.publish`.
10. **The frozen real run is the main end-to-end data (new):** the worker is tested on `catcher testdata reset` data (`tests/data`), which now carries the saved facts and replies, so no fake profile and no cost are needed; a hand test needs no `--profile fake` either (the `profile` param stays available).

## Global Constraints

- Stage A behaviour is frozen: `uv run pytest tests/unit tests/component tests/integration/git -q` stays green, **existing tests unchanged** (moving shared fixtures up into a new `tests/integration/conftest.py` in Task 6 is the one allowed edit of test support code, with identical behaviour). New tests are added.
- Every queue, item and worker function takes `now: datetime` (aware) from the caller; **never SQL `now()`**. Tests use a frozen clock.
- Queue rules as built: job status `queued/running/succeeded/failed/cancelled`; higher `priority` first; claim order `priority DESC, run_after, created_at`; fencing token `(locked_by, claim_seq)`; `attempts` counts crashes only; `defer` gives an attempt back.
- Item key = `calculated_name` = `<subfolder>/<name>.md` (`note.target_rel.as_posix()`); a requeue resets the existing row, it never inserts a second one.
- **DB row first, then the file move** (decision 5). A handler is idempotent: running it twice (after a crash or a lost lease) yields the same files and rows.
- No live LLM or YouTube/yt-dlp call in any test (fake backends, fake fetchers, the frozen saved replies and facts). The only network use is the Postgres image. The implementer runs the whole suite once per task with outgoing network blocked if a check script for that exists; otherwise a backend and a fetcher that raise when called are the proof.
- The real-looking test data is `tests/data` (inbox of 28 notes, 15 clippings, 1 PDF, plus the saved `facts/` and `llm/` and the approved pages in `tests/data/expected/`). Never edit it. Prompt versions in the saved replies must still match (a prompt version bump makes them miss: re-record, do not edit). Docker must be running for the db tests; they run with `uv run pytest tests/integration/db -q` and are not in the pre-commit hook.
- Sync SQLAlchemy 2 + psycopg 3; line length 110; ruff (`E F I UP B SIM`); pyright standard on `src`.
- Commit only on the user's go (executors commit locally on `stage-b` when the plan is approved; no push); end commit messages with the attribution line the session requires.

## Review Focus

Failure modes most likely to bite, each pinned by a test in the named task:

1. **Crash between the staging row and the file move** (or between the move and the next job): the next `pipeline.run` adopts the leftover row and no document is lost or archived twice (Tasks 8, 13).
2. **Crash after the page is written but before the job completes:** the re-run overwrites the same page by id, no duplicate in `epiaku-docs`, the item ends `published`, and the **model is called once in total** (the re-run reuses the saved reply) (Tasks 9, 13).
3. **An LLM failure causes a YouTube call:** it must not; facts are fetched once, by `youtube.fetch`, and a deferred-then-retried item reads the saved facts (Task 12).
4. **A handler outlives its lease:** the heartbeat keeps it alive; if the lease is lost anyway the result is discarded (fencing) and the handler's effects are safe to repeat (Tasks 2, 13).
5. **A handler raises an unexpected exception:** the job is `failed` with the error, the item is `failed`, the worker keeps running (Tasks 1, 3).
6. **A second worker starts:** it refuses to run (Task 4).

---

## Part A: the worker core

### Task 1: the handler contract and job dispatch

**Files:**
- Create: `src/catcher/modules/worker/__init__.py`, `src/catcher/modules/worker/handlers.py`, `src/catcher/modules/worker/dispatch.py`
- Test: `tests/integration/db/test_worker_dispatch.py`

**Interfaces:**
- Produces in `handlers.py`: `@dataclass(frozen=True) class Done: result: dict[str, Any] | None = None`; `class Defer: run_after: datetime; reason: str`; `class Fail: error: str`; `HandlerResult = Done | Defer | Fail`; `@dataclass class HandlerContext: settings: Settings; services: Services; engine: Engine; ideas: Path; docs: Path; clock: Callable[[], datetime]`; `Handler = Callable[[HandlerContext, Job], HandlerResult]`.
- Produces in `dispatch.py`: `def run_job(ctx: HandlerContext, job: Job, handlers: Mapping[str, Handler], *, lease_s: float, heartbeat_s: float) -> Literal["succeeded", "deferred", "failed", "lost"]`. It runs the handler (inside the `Heartbeat` of Task 2: until Task 2 exists, leave a no-op seam), maps the result to `queue.complete/defer/fail` in a **fresh `session_scope`**, and returns `"lost"` when the fenced call returns False (the result is discarded). An unknown `job.type` -> `fail("no handler for <type>")`. A handler `Exception` -> `fail(f"{type(e).__name__}: {e}")` with `log.exception`. `KeyboardInterrupt`/`SystemExit` propagate (the lease expires and the reaper requeues).

- [ ] **Step 1: Write the failing test.** `test_done_completes_the_job_with_its_result`; `test_defer_requeues_without_counting_an_attempt`; `test_fail_marks_the_job_failed`; `test_a_handler_exception_fails_the_job_and_does_not_propagate`; `test_an_unknown_job_type_fails_the_job`; `test_a_result_after_a_lost_lease_is_discarded` (claim as w1, advance past the lease, `reap`, claim as w2, then `run_job` for w1's stale Job returns `"lost"` and the row is unchanged); `test_keyboard_interrupt_propagates_and_leaves_the_job_running`.
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_worker_dispatch.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the dataclasses and `run_job` (a dict dispatch on `job.type`; one `session_scope` for the finishing call).
- [ ] **Step 4: Run** the same command plus `uv run pyright`. Expected: PASS, 0 errors.
- [ ] **Step 5: Commit** `feat: the handler contract and job dispatch`.

### Task 2: the heartbeat thread

**Files:**
- Create: `src/catcher/modules/worker/heartbeat.py`; Modify: `dispatch.py` (use it)
- Test: `tests/integration/db/test_worker_heartbeat.py`

**Interfaces:**
- Produces: `class Heartbeat: def __init__(self, engine: Engine, job: Job, *, lease_s: float, interval_s: float, clock: Callable[[], datetime]) -> None`; a context manager (`__enter__` starts a daemon thread, `__exit__` stops and joins it); property `lost: bool` (True once a beat returned False). The thread uses **its own session per beat** and its **own snapshot of the Job token** (a detached copy: never the Job object the handler is reading, so there is no cross-thread mutation).

- [ ] **Step 1: Write the failing test.** `test_a_long_handler_keeps_its_lease_alive` (handler sleeps past the original lease with `lease_s=0.6`, `interval_s=0.1`, real clock: after it the lease is still in the future and `reap` changes nothing); `test_the_thread_stops_with_the_context`; `test_a_lost_lease_sets_lost` (another claim takes the job: `lost` becomes True and the thread stops); `test_a_failing_beat_is_logged_and_the_thread_keeps_trying` (inject a `session_scope` failure once). Use daemon threads and `assert not thread.is_alive()` after exit.
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_worker_heartbeat.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `Heartbeat` with a `threading.Event` wait loop; `dispatch.run_job` wraps the handler call in it and treats `lost` as `"lost"` even when the handler returned.
- [ ] **Step 4: Run** the new tests 5 times in a row and `tests/integration/db/test_worker_dispatch.py`. Expected: PASS, stable.
- [ ] **Step 5: Commit** `feat: a heartbeat thread keeps a running job's lease`.

### Task 3: the worker loop

**Files:**
- Create: `src/catcher/modules/worker/loop.py`
- Test: `tests/integration/db/test_worker_loop.py`

**Interfaces:**
- Produces: `class Worker: def __init__(self, ctx: HandlerContext, handlers: Mapping[str, Handler], *, worker_id: str, lease_s: float = 120.0, heartbeat_s: float = 30.0, poll_s: float = 2.0, reap_every_s: float = 60.0, sleep: Callable[[float], None] = time.sleep) -> None`; `def run_once(self) -> str | None` (claim one job in its own committed session, `run_job`, return the dispatch label; `None` when nothing is due); `def reap_safely(self) -> int` (calls `queue.reap` in its own session; any exception is logged and returns 0); `def run_forever(self, stop: threading.Event) -> None` (reap at start, then loop: `run_once`, when idle `sleep(poll_s)`, reap every `reap_every_s`; finish the current job after `stop` is set, then return). Also `def make_worker_engine(url: str) -> Engine` in `core/db.py` (psycopg option `lock_timeout=10000`).

- [ ] **Step 1: Write the failing test.** `test_run_once_returns_none_when_idle`; `test_run_once_runs_one_job_per_call_in_priority_order`; `test_a_failing_handler_does_not_stop_the_worker` (job 1 raises, job 2 still runs: Review Focus #5); `test_a_deferred_job_is_not_picked_up_again_before_its_time` (frozen clock); `test_the_reaper_requeues_a_crashed_job_at_start` (leave a running job with an expired lease, `run_forever` with a stop event set from the handler: the job runs); `test_reap_safely_survives_a_database_error` (monkeypatch `reap` to raise once: `run_forever` continues); `test_stop_lets_the_current_job_finish`.
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_worker_loop.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `Worker` and `make_worker_engine`. The clock comes from `ctx.clock`; the real loop passes `utc_now`.
- [ ] **Step 4: Run** the new file 3 times in a row and `uv run pyright`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: the worker loop with a tolerant reaper`.

### Task 4: one worker at a time

**Files:**
- Create: `src/catcher/modules/worker/guard.py`
- Test: `tests/integration/db/test_worker_guard.py`

**Interfaces:**
- Produces: `class WorkerAlreadyRunning(RuntimeError)`; `class WorkerLock: def __init__(self, engine: Engine, key: int = 0x636174636865) -> None`; context manager that takes a session-level Postgres advisory lock (`pg_try_advisory_lock(key)`) on a **dedicated connection** and holds it until exit; raises `WorkerAlreadyRunning` when another holder has it.

- [ ] **Step 1: Write the failing test.** `test_a_second_worker_lock_is_refused` (Review Focus #6); `test_the_lock_is_released_on_exit_and_can_be_taken_again`; `test_the_lock_is_released_when_the_holder_connection_dies` (terminate the holder's backend with `pg_terminate_backend`, then the lock can be taken).
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_worker_guard.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `WorkerLock` with `engine.connect()` kept open for the lifetime.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat: refuse a second worker with an advisory lock`.

### Task 5: `catcher worker` and `catcher jobs`

**Files:**
- Modify: `src/catcher/cli.py`; Create: `src/catcher/modules/worker/app.py` (builds the `HandlerContext` and the handler registry: `build_handlers() -> dict[str, Handler]`, empty until Part B fills it, and `build_context(settings) -> HandlerContext` using `default_services`)
- Test: `tests/integration/db/test_worker_cli.py`

**Interfaces:**
- Produces CLI: `catcher worker [--once] [--ideas PATH] [--docs PATH] [--lease-s FLOAT] [--poll-s FLOAT]` (takes the `WorkerLock`, installs SIGTERM/SIGINT -> `stop.set()`, `--once` drains the due jobs and exits); `catcher jobs add TYPE [--param KEY=VALUE]... [--priority INT]` (prints the job id; `KEY=true/false/int` parsed; **refuses a `dry_run` param with exit 2**); `catcher jobs list [--status STATUS] [--limit N]` (one line per job: id, type, status, priority, run_after, reason).

- [ ] **Step 1: Write the failing test.** With CliRunner and `DATABASE_URL` set to a migrated fresh database: `test_jobs_add_enqueues_and_prints_the_id`; `test_jobs_add_refuses_dry_run`; `test_jobs_list_filters_by_status`; `test_worker_once_runs_a_registered_handler_and_exits` (register a test handler through a small seam: `catcher.modules.worker.app.EXTRA_HANDLERS` dict, used by tests only); `test_a_second_worker_command_is_refused_with_exit_2`.
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_worker_cli.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the commands (sessions via `session_scope(make_worker_engine(...))`).
- [ ] **Step 4: Run** `uv run pytest tests/unit tests/component tests/integration/db -q` and `uv run pyright`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: catcher worker and catcher jobs`.

---

## Part B: the handlers

### Task 6: shared repo fixtures and a worker test harness

**Files:**
- Create: `tests/integration/conftest.py` (the git helpers `_sh`, the `git_identity` autouse fixture, `sh`, `make_repo` moved up from `tests/integration/git/conftest.py`; delete them there so there is one definition); Create: `tests/integration/db/harness.py` (a `WorkerHarness`: tmp idea-bucket and epiaku-docs repos from the same seed as `tests/integration/git/test_run.py` (a note, a Gemini chat, a YouTube clip), a `HandlerContext` with `make_services`-style fake backends and a **counting fake YouTube fetcher** behind a real `YoutubeAccess` and a tmp-dir gate, a frozen clock, `drain(max_jobs=50)` that calls `Worker.run_once` until idle and returns the labels, and `jobs(session)` helper)
- Also: a second fixture `frozen_harness` in the same harness module: the same machinery on `reset_test_repos(tmp_path)` of the committed `tests/data` (real inbox, saved facts and replies), with a model backend factory and a YouTube fetcher that **raise if called** (recording the attempt in `.model_calls` / `.fetch_calls`)
- Test: `tests/integration/db/test_harness.py`

**Interfaces:**
- Produces: fixture `harness` (in `tests/integration/db/conftest.py`) giving `.ideas`, `.docs` (Paths), `.ctx` (HandlerContext), `.worker`, `.fetch_calls: list[str]`, `.backends` (the fake note/chat backends), `.drain() -> list[str]`, `.add_job(type, **params) -> uuid.UUID`; fixture `frozen_harness` with the same attributes plus `.model_calls`.

- [ ] **Step 1: Write the failing test.** `test_the_harness_builds_two_git_repos_and_an_idle_worker`; `test_drain_returns_no_labels_when_there_are_no_jobs`; `test_the_frozen_harness_starts_with_the_committed_inbox_facts_and_replies` (44 inbox files, 2 facts, 43 traces, and calling the model or YouTube is recorded); `test_the_git_tests_still_pass_after_the_fixtures_moved` (this one is just the existing git suite run: Step 4).
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_harness.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the move and the harness (reuse the `make_services` fixture logic from `tests/conftest.py`; the YouTube fixture facts come from the `yt_facts` fixture).
- [ ] **Step 4: Run** `uv run pytest tests/integration/git -q` **unchanged and green**, then `uv run pytest tests/integration/db -q`. Expected: PASS.
- [ ] **Step 5: Commit** `test: share the git fixtures and add a worker harness`.

### Task 7: the item API

**Files:**
- Create: `src/catcher/modules/queue/items.py`
- Test: `tests/integration/db/test_items.py`

**Interfaces:**
- Produces: `class ItemExists(Exception)`; `def stage_item(session: Session, *, calculated_name: str, doc_id: str, doc_class: str, now: datetime, inbox_path: str | None, original_filename: str | None, root_job_id: uuid.UUID | None = None, origin: str = "inbox") -> JobItem` (status `staging`; **a row in a terminal status (`published`, `deferred`, `failed`, `stuck`, `duplicate`) is reset** to `staging`; a row in an active status (`staging`, `waiting_youtube`, `waiting_llm`, `ready`) raises `ItemExists`); `def set_item_status(session, calculated_name: str, status: str, *, now: datetime, reason: str | None = None) -> JobItem | None` (None when the row does not exist; sets `updated_at`, `stage_reason` is NOT a column yet: keep the reason in `error` for `failed`/`deferred`, `None` otherwise); `def get_item(session, calculated_name: str) -> JobItem | None`; `def items_in_status(session, *statuses: str) -> list[JobItem]`.

- [ ] **Step 1: Write the failing test.** `test_stage_item_creates_a_staging_row`; `test_a_second_stage_of_an_active_item_raises`; `test_a_terminal_item_is_reset_by_a_requeue_not_duplicated`; `test_set_item_status_updates_status_and_reason`; `test_set_item_status_of_a_missing_row_returns_none`; `test_two_connections_staging_the_same_name_get_one_row` (threads + barrier, daemon threads).
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_items.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** with a guarded insert (unique violation -> check the existing status -> reset or raise).
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat: item rows for staged documents`.

### Task 8: the `pipeline.run` handler (staging, adoption)

**Files:**
- Create: `src/catcher/modules/worker/handlers_pipeline.py`; Modify: `worker/app.py` (register)
- Test: `tests/integration/db/test_handler_pipeline_run.py`

**Interfaces:**
- Consumes: `scan_inbox`, `order_notes`, `split_duplicates`, `requeue_from_archive`, `deferred_in_output`, `move_to_failed`, `move_to_duplicates`, `copy_artifacts`, `start_work`, `assign_name`, `FACTS_DIR`; Task 7's item API; `queue.enqueue`.
- Produces: `def handle_pipeline_run(ctx: HandlerContext, job: Job) -> HandlerResult`. Params: `only: list[str] | None`, `requeue: list[str] | None`, `retry_deferred: bool`, `limit: int | None`, `profile: str | None` (the LLM profile, for example `fake`; copied into the `params` of every `llm.reason` job this run enqueues, like `--profile` in Stage A), `refresh_llm: bool = False` (copied the same way: the CLI's `--refresh-llm`). Per document: unreadable -> `failed/` (as Stage A); duplicates -> `duplicates/`; artifacts -> `copy_artifacts`; every other note: **(1)** `assign_name`, `stage_item(...)` committed (status `staging`), **(2)** `start_work`, **(3)** item status `waiting_youtube` (class `youtube` with no saved facts) or `waiting_llm`, and `enqueue` `youtube.fetch` / `llm.reason` with `params={"calculated_name": ...}` and `dedupe_key=f"fetch:{name}"` / `f"reason:{name}"`, in one commit. A `youtube` clip whose facts are already saved goes straight to `llm.reason`. **Adoption** at the start of every run: for each item in `staging` (a single worker means any such row is a crash leftover): if `output/<name>` exists, continue at step 3; else if the inbox file named by `inbox_path` exists, rebuild the Note with the **same calculated name**, run `start_work` (it overwrites a partial archive copy) and continue at step 3; else mark the item `failed` ("staging row without a document"). Returns `Done({"staged": n, "adopted": n, "duplicates": n, "artifacts": n, "unreadable": n})`.

- [ ] **Step 1: Write the failing test.** On the harness: `test_run_stages_a_note_and_enqueues_llm_reason` (inbox empty, `output/` copy with `stage: analyzed`, archive copy, item `waiting_llm`, one `llm.reason` job); `test_a_youtube_clip_is_staged_waiting_youtube_with_a_fetch_job`; `test_a_youtube_clip_with_saved_facts_goes_straight_to_llm_reason`; `test_running_twice_stages_nothing_twice` (dedupe keys + empty inbox); `test_duplicates_unreadable_and_artifacts_are_handled_like_stage_a` (same folders and statuses as the Stage A tests); `test_a_crash_after_the_staging_row_is_adopted_with_the_same_name` (monkeypatch `start_work` to raise once after the row is committed, then run again: one archive copy, one output copy, one job, same calculated name: Review Focus #1); `test_a_crash_after_the_move_before_the_job_is_adopted` (monkeypatch `enqueue` to raise once); `test_requeue_resets_the_existing_item` (`requeue` param on a published note: same name, status back to `waiting_llm`, still one row); `test_retry_deferred_requeues_stalled_items`.
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_handler_pipeline_run.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the handler; reuse the Stage A functions (do not copy their logic); `limit` counts staged notes.
- [ ] **Step 4: Run** the same command and `uv run pytest tests/integration/git -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: the pipeline.run handler stages documents database-first`.

### Task 9: the `llm.reason` handler

**Files:**
- Modify: `src/catcher/modules/pipeline/process.py` (`ProcessOptions.allow_fetch: bool = True`, and `facts_for` uses `fetch_allowed=opts.allow_fetch and not opts.dry_run`; and a shared function for the invalid-page marking that today sits inline in `run.py` (`mark_unusable` of the trace that made an invalid page), used by `run.py` and the handler, Stage A tests unchanged), `src/catcher/modules/pipeline/run.py` (use the shared function), `worker/handlers_pipeline.py`, `worker/app.py`
- Test: `tests/integration/db/test_handler_llm_reason.py`, `tests/component/test_process_steps.py` (one new test for `allow_fetch`)

**Interfaces:**
- Produces: `def handle_llm_reason(ctx: HandlerContext, job: Job) -> HandlerResult`. Param `calculated_name`. It loads the item (missing -> `Fail`), `load_staged_note(ctx.ideas, ctx.ideas / "output" / name)`, runs `get_facts` with `ProcessOptions(profile=job.params.get("profile"), refresh_llm=bool(job.params.get("refresh_llm")), allow_fetch=False, facts_dir=ctx.ideas / FACTS_DIR, llm_dir=ctx.ideas / LLM_DIR)` (so `ask_llm` reads a good saved reply and records a trace like Stage A; the trace file joins the commit set), `ask_llm`, `build_page`; on success `write_page` + `finish` and item `published`; on an exception `classify(e)`: `deferred` -> `mark_deferred` + item `deferred` (reason stored), `failed` -> `move_to_failed` + item `failed`, `would_fetch` (facts not saved) -> item `waiting_youtube` + enqueue `youtube.fetch` (dedupe key), job `Done`; the **job** ends `Done({"item": status})` in all these cases (decision 3). Reuse `apply_outcome` for the file effects.

- [ ] **Step 1: Write the failing test.** `test_llm_reason_publishes_a_note_page_and_marks_the_item_published` (page in docs repo, final page in `output/`, no `stage`); `test_the_same_job_run_twice_gives_one_page` (Review Focus #2: run the handler twice: one page in docs, same file); `test_a_backend_outage_defers_the_item_and_succeeds_the_job` (`stage: deferred` in `output/`, item `deferred`, job `succeeded`); `test_invalid_output_moves_the_note_to_failed`; `test_a_youtube_clip_without_saved_facts_requeues_the_fetch_and_makes_no_call` (`fetch_calls == []`); `test_a_youtube_clip_with_saved_facts_publishes`; `test_a_missing_item_fails_the_job`; `test_a_second_run_reuses_the_saved_reply_and_never_calls_the_model` (a backend that raises proves it); `test_refresh_llm_in_the_job_params_calls_the_model_again`; `test_an_invalid_page_marks_the_trace_invalid_page_and_a_requeue_calls_the_model`; `test_the_trace_file_is_written_next_to_the_page`. In `test_process_steps.py`: `test_allow_fetch_false_never_calls_youtube_even_outside_a_dry_run`.
- [ ] **Step 2: Run** the two files. Expected: FAIL.
- [ ] **Step 3: Implement** the option and the handler.
- [ ] **Step 4: Run** the new tests and `uv run pytest tests/unit tests/component tests/integration/git -q`. Expected: PASS, Stage A unchanged.
- [ ] **Step 5: Commit** `feat: the llm.reason handler`.

### Task 10: the `youtube.fetch` handler

**Files:**
- Modify: `src/catcher/modules/youtube/facts.py` (`FactsDeferred` gets `until: float | None = None`), `youtube/access.py` (both raises pass `until`), `worker/handlers_pipeline.py`, `worker/app.py`
- Test: `tests/integration/db/test_handler_youtube_fetch.py`, `tests/component/test_youtube_access.py` (one new test: `FactsDeferred.until` is the gate's next-allowed time)

**Interfaces:**
- Produces: `FactsDeferred(message: str, until: float | None = None)` (existing call sites unchanged); `def handle_youtube_fetch(ctx: HandlerContext, job: Job) -> HandlerResult`. Param `calculated_name`. It loads the staged note, `svc.youtube.get(vid, facts_dir=ctx.ideas / FACTS_DIR, wait=False)`: success -> item `waiting_llm`, `enqueue llm.reason` (dedupe key), `Done`; `FactsDeferred` -> **`Defer(run_after=datetime.fromtimestamp(e.until, UTC), reason=str(e))`** (the job waits, the item stays `waiting_youtube`, no attempt counted); `FactsUnavailable` (no transcript, a gone video, a yt-dlp failure) -> `mark_deferred` + item `deferred`, `Done`; an unexpected exception -> `classify` -> item `failed`.

- [ ] **Step 1: Write the failing test.** `test_fetch_saves_the_facts_and_enqueues_llm_reason`; `test_a_closed_gate_defers_the_job_until_the_next_slot` (a second fetch right after the first: job `queued` with `run_after` = the slot, `attempts` unchanged, item still `waiting_youtube`); `test_a_429_opens_the_breaker_and_defers_without_counting_an_attempt`; `test_no_transcript_defers_the_item_not_the_job`; `test_saved_facts_mean_no_second_call` (`fetch_calls` has one entry after a requeue + fetch). Component: `test_facts_deferred_carries_the_gate_time`.
- [ ] **Step 2: Run** the two files. Expected: FAIL.
- [ ] **Step 3: Implement** the attribute, both raise sites and the handler.
- [ ] **Step 4: Run** the new tests and the Stage A suites. Expected: PASS.
- [ ] **Step 5: Commit** `feat: the youtube.fetch handler defers on the gate`.

### Task 11: the `pipeline.publish` handler and the managed-folder commit

**Files:**
- Modify: `src/catcher/core/git.py` (`commit_managed`), `worker/handlers_pipeline.py`, `worker/app.py`
- Test: `tests/integration/git/test_commit_managed.py` (new file; Stage A tests untouched), `tests/integration/db/test_handler_publish.py`

**Interfaces:**
- Produces in `git.py`: `def commit_managed(repo: Path, pathspecs: Sequence[str], message: str, *, author: tuple[str, str]) -> bool`: `git add -A -- <existing pathspecs>` (so deletions in `inbox/` are staged too), returns False when nothing under those paths changed, else commits **only those paths** (`git commit -m ... -- <pathspecs>`) with the author, leaving unrelated changes uncommitted. Produces `def handle_pipeline_publish(ctx, job) -> HandlerResult`: params `pull: bool = True`, `push: bool = True`; pull both repos (a `GitError` -> `Fail` with the message, nothing committed), `commit_managed` on the idea-bucket (`inbox archive output failed duplicates facts llm`) and on epiaku-docs (the distinct `out_dir` of `DOC_TYPES` plus `idea-bucket/artifacts`), push **right after each commit** (decision 6); `Done({"committed": {"docs": bool, "ideas": bool}, "pushed": bool})`.

- [ ] **Step 1: Write the failing test.** `test_commit_managed_commits_only_the_given_folders` (an unrelated change stays dirty); `test_commit_managed_stages_deletions_and_moves`; `test_commit_managed_returns_false_when_nothing_changed`. Handler: `test_publish_commits_both_repos_and_pushes_to_the_remotes`; `test_a_failed_pull_fails_the_job_and_changes_nothing`; `test_publish_twice_makes_no_second_commit`; `test_unrelated_docs_changes_are_not_committed`.
- [ ] **Step 2: Run** the two files. Expected: FAIL.
- [ ] **Step 3: Implement** `commit_managed` and the handler (author from `settings.git_author_*`).
- [ ] **Step 4: Run** `uv run pytest tests/integration -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: the pipeline.publish handler commits the managed folders`.

### Task 12: end to end on the frozen real run, and no YouTube call after an LLM failure

**Files:**
- Create: `tests/integration/golden.py` (the page comparison of `tests/integration/git/test_testdata_run.py` moved into a shared helper: `compare_pages(published_dir, expected_dir)` matching by frontmatter `original_filename` and dropping only the volatile keys (`source_file`; `id` and `date` for notes); the existing test imports it, behaviour unchanged: the one allowed edit of that test file)
- Test: `tests/integration/db/test_worker_end_to_end.py`

- [ ] **Step 1: Write the failing test.** `test_the_worker_publishes_the_frozen_real_run_with_no_external_call` (the main one): on `frozen_harness`: `pipeline.run`, `drain()`, `pipeline.publish`: 43 published pages equal the approved pages in `tests/data/expected/` (via `compare_pages`), 1 artifact, `model_calls == []`, `fetch_calls == []`, every `llm/` and `facts/` file byte-identical, nothing left in `inbox/`, both repos clean after the publish commit, every item row `published`. `test_the_worker_matches_stage_a_on_the_same_data`: Stage A `run_pipeline` on one reset copy and the worker path on another give the same pages (compare by `compare_pages` against each other). `test_an_llm_failure_makes_no_youtube_call` (Review Focus #3): on a copy of the frozen data with ONE direct YouTube clip's saved facts deleted and its saved reply deleted, a fetcher that returns that clip's frozen facts JSON (counting calls) and a chat backend raising `BackendUnavailable`: after `pipeline.run` + `drain()` `fetch_calls` has exactly one entry and the item is `deferred`; `pipeline.run` with `retry_deferred` + a working backend + `drain()` publishes it and `fetch_calls` is **still one** (the saved facts are read). `test_a_closed_gate_waits_in_the_queue_and_the_next_slot_publishes` (two direct clips with the saved facts of both deleted, a real file gate that allows one fetch: the second fetch job is deferred until the frozen clock passes the slot, then the page appears; no attempt counted).
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_worker_end_to_end.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** only what the failing tests expose (the helper move; real differences go to the owning handler with a test in that task's file, never by loosening the comparison).
- [ ] **Step 4: Run** the same command, `uv run pytest tests/integration/git/test_testdata_run.py -q` (unchanged behaviour), then `uv run pytest tests/unit tests/component tests/integration -q`. Expected: PASS.
- [ ] **Step 5: Commit** `test: the worker publishes the frozen real run`.

### Task 13: crash injection

**Files:**
- Test: `tests/integration/db/test_worker_crashes.py`

- [ ] **Step 1: Write the failing test.** Each with the harness and a real reaper:
  - `test_a_worker_killed_mid_llm_reason_is_recovered`: the handler raises `SystemExit` after `write_page` but before `complete` (the lease is left to expire), advance the frozen clock past the lease, `reap`, run again: **one** page in docs (overwritten by id), the item `published`, `attempts == 2`, not duplicated, and a counting model backend was called **exactly once across the crash and the re-run** (the saved reply is reused) (Review Focus #2).
  - `test_a_handler_that_outlives_its_lease_is_not_reaped_while_it_beats` and `test_a_lost_lease_discards_the_result_and_the_repeat_is_harmless` (Review Focus #4: the stale worker's `complete` is refused, the second run's files equal the first's).
  - `test_a_poison_clip_fails_after_max_attempts` (a handler that always `SystemExit`s: after 3 expired leases the job is `failed`, the item is `failed` and the other jobs still run).
  - `test_staging_leftovers_are_adopted_after_a_worker_restart` (kill between `stage_item` and `start_work`, start a fresh `Worker`, `pipeline.run`: Review Focus #1 end to end).
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_worker_crashes.py -q`. Expected: FAIL where a handler or the loop does not recover.
- [ ] **Step 3: Implement** the fixes the tests expose, each in the owning module (with a test in that task's file).
- [ ] **Step 4: Run** the file 3 times in a row, then `uv run pytest tests/unit tests/component tests/integration -q`. Expected: PASS, stable.
- [ ] **Step 5: Commit** `test: crash injection for the worker and its handlers`.

### Task 14: docs and the carried minors

**Files:**
- Modify: `docs/idea-catcher-how-to-run.md` (a "Worker (Stage B)" section: start Postgres, `db upgrade`, `catcher jobs add pipeline.run`, `catcher worker --once`, `catcher jobs list`, `catcher jobs add pipeline.publish`; stop with Ctrl-C; one worker only), `docs/idea-catcher-configuration.md` (any new setting; none planned), `docs/idea-catcher-service-architecture.md` (B3 built with the date; the transaction rules; what changed from the plan; update "Minors carried into B3": the reaper timer, `lock_timeout` in worker sessions and the `doc_id`/`doc_class` question are now settled (both are set by `_analyse` before staging); note the new open items), `docs/idea-catcher-diagram-processing-loop.md` or `-architecture.md` (a diagram of the worker path if it fits: validate it with the Mermaid checker), `README.md` (`catcher worker`, `catcher jobs` rows)

- [ ] **Step 1: Verify** each documented command against a real throwaway Postgres and the test repos that `catcher testdata reset` makes in `tmp/ic`, as a recipe: `uv run catcher testdata reset` (its repos now hold the frozen saved facts and replies, so the run is free: no model and no YouTube call), `uv run catcher db upgrade`, `uv run catcher jobs add pipeline.run`, `OPENAI_API_KEY="" FREELLMAPI_URL=http://127.0.0.1:1/v1 YOUTUBE_OFFLINE=1 uv run catcher worker --once --ideas tmp/ic/idea-bucket --docs tmp/ic/epiaku-docs` (an empty key and an unreachable URL make a cache miss fail instead of costing money), `uv run catcher jobs list`, `uv run catcher jobs add pipeline.publish` + the worker again; and `jobs add pipeline.run --param refresh_llm=true` for a fresh call. Do NOT run these against the user's `tmp/ic` without asking: use your own throwaway folder (see how Task 6 of the saved-replies plan did it).
- [ ] **Step 2: Write** the doc changes; keep them proportionate; check any Mermaid block with the validator.
- [ ] **Step 3: Run** `uv run pytest tests/unit tests/component tests/integration -q`. Expected: PASS.
- [ ] **Step 4: Commit** `docs: Stage B3 built`.
