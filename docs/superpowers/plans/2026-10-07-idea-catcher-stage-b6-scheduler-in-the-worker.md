# Idea Catcher Stage B6: the Scheduler in the Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `catcher worker` fires three cron schedules (pull idea-bucket, run the pipeline, publish) by queueing jobs, runs a missed slot once, and a manual `catcher publish` exists.

**Architecture:** A pure module decides which slot is due (cron string + timezone + `last_fired_at` + now). A `Scheduler` ticks every 30 s in a thread of `catcher worker` (not with `--once`): in one transaction it locks the `schedules` row, queues the job and sets `last_fired_at`. The scheduler only enqueues; the worker's one-job-at-a-time loop does the work. The worker's one-worker lock means there is only ever one scheduler.

**Tech Stack:** Python 3.12, SQLAlchemy 2 (sync) + psycopg 3, Alembic (no new migration: the `schedules` table exists since `0001`), Typer, `croniter` and `tzdata` (new dependencies), pytest + testcontainers (real Postgres, fake clock).

**Spec:** `docs/idea-catcher-service-architecture.md`: row B6 of the Stage B table (about line 715), "Scheduling" (about line 362: the three variables, "every 30 s", "a missed schedule once", the dedupe rule), Stage B decision 7 (about line 696: cron strings in `.env`, an explicit timezone, one scheduler, `last_fired_at`), and the B5 note that `retry_deferred` is added by the scheduler (about line 916).

## Global Constraints

- Postgres is the single truth: no schedule state in files; if the database is down the worker does not run (unchanged).
- Cron syntax by `croniter`; local time in an **explicit** timezone (`SCHEDULE_TIMEZONE`); the loop checks every 30 s (`SCHEDULE_TICK_S` default 30).
- Three variables, exactly: `SCHEDULE_IDEAS_PULL`, `SCHEDULE_PIPELINE_RUN`, `SCHEDULE_PUBLISH`. An empty value (the default) means that schedule is off.
- A missed schedule runs **once**, not once per missed slot.
- The scheduler only enqueues jobs. A scheduled `pipeline.run` carries `retry_deferred: true`.
- No live LLM or YouTube call in any test; the network guard stays on; db tests use testcontainers, never the user's `catcher-db`.
- Agents commit locally, never push (the user pushes). Never touch `tests/data`, `tmp/ic`, `~/.catcher`, `.env`.

## Decisions made in this plan (defaults; the user can change them)

1. **Timezone default `Europe/Amsterdam`** (`SCHEDULE_TIMEZONE`), validated with `zoneinfo`; `tzdata` is a dependency so the Docker image has zones.
2. **A new schedule starts "now":** with no `schedules` row the scheduler creates it with `last_fired_at = now` and fires nothing (a fresh install does not run at once; the next slot fires). After downtime the row exists, so exactly one catch-up fires.
3. **Job types and params:** `SCHEDULE_IDEAS_PULL` queues a new job type **`ideas.pull`** (no params; `pipeline.run` never pulled before, so there is nothing to reuse); `SCHEDULE_PIPELINE_RUN` queues `pipeline.run` `{"retry_deferred": true}`; `SCHEDULE_PUBLISH` queues `pipeline.publish` `{"pull": true, "push": true}`.
4. **Dedupe:** each schedule uses the dedupe key `schedule:<name>`. If its previous job is still queued or running, no second job is queued, but `last_fired_at` still advances (the slot counts as handled; this is the "runs once" rule).
5. **Manual `catcher publish [--push]`** mirrors `run pipeline`: commit only unless `--push` (then `pull` and `push` both true). It takes the worker lock, queues `pipeline.publish`, drains, prints committed/pushed, same exit codes and refusals as `run pipeline`.
6. **A bad cron string or timezone** stops `catcher worker` before it takes the lock: message names the variable, exit 2.
7. **Clock goes backwards** (`last_fired_at` in the future): nothing fires until `now` passes it; no error.

## Review Focus

- A slot that does not exist or exists twice because of daylight saving time (02:30 on the spring-forward day; 02:30 on the fall-back day): fires once, never twice, never skipped silently on the day itself.
- Three days of downtime with `*/30 * * * *`: exactly one job per schedule on the first tick, none on the second tick of the same minute.
- The previous scheduled `pipeline.run` is still running when the next slot arrives: no second job, the slot is consumed, nothing piles up.
- `SCHEDULE_PIPELINE_RUN="0 8 * * * *"` (six fields) or `"every day"` or `SCHEDULE_TIMEZONE="Mars/Base"`: refused at start with a message naming the variable.
- `ideas.pull` on a checkout without a remote or with a dirty tree: succeeds as a no-op / autostashes; a pull conflict fails the job with git's message and leaves no rebase in progress.
- The database is unreachable during a tick: the thread logs, backs off to the next tick and the worker keeps its other duties; it never dies silently.

## File structure

- Create `src/catcher/modules/scheduler/__init__.py`, `schedule.py` (pure: parse + due slot), `loop.py` (`Scheduler`: tick, thread helper).
- Create `tests/unit/test_schedule_rules.py`, `tests/integration/db/test_scheduler.py`, `tests/integration/db/test_handler_ideas_pull.py`, `tests/integration/db/test_schedules_cli.py`, `tests/integration/db/test_publish_command.py`.
- Modify `src/catcher/core/config.py` (four settings), `pyproject.toml` (`croniter`, `tzdata`), `src/catcher/modules/worker/handlers_pipeline.py` and `app.py` (`ideas.pull`), `src/catcher/modules/worker/runner.py` (`publish_command`), `src/catcher/cli.py` (`worker` starts the scheduler, `schedules` command, `publish` command), docs, `.env.example` if the repo has one.

---

### Task 1: Settings and the pure schedule rules

**Files:**
- Modify: `src/catcher/core/config.py`, `pyproject.toml` (run `uv add croniter tzdata`)
- Create: `src/catcher/modules/scheduler/__init__.py`, `src/catcher/modules/scheduler/schedule.py`
- Test: `tests/unit/test_schedule_rules.py`

**Interfaces:**
- Produces in `Settings`: `schedule_ideas_pull: str = ""`, `schedule_pipeline_run: str = ""`, `schedule_publish: str = ""`, `schedule_timezone: str = "Europe/Amsterdam"`, `schedule_tick_s: float = Field(default=30, gt=0)`.
- Produces in `schedule.py`:
  - `@dataclass(frozen=True) class ScheduleSpec: name: str; cron: str; job_type: str; params: dict[str, Any]; variable: str`
  - `SCHEDULE_NAMES = ("ideas_pull", "pipeline_run", "publish")`
  - `def parse_schedules(settings: Settings) -> list[ScheduleSpec]`: one spec per non-empty variable; raises `ValueError` naming the variable for a bad cron (five fields only, valid for `croniter`) or an unknown timezone.
  - `def due_slot(spec: ScheduleSpec, tz: ZoneInfo, last_fired_at: datetime, now: datetime) -> datetime | None`: the latest slot `s` with `last_fired_at < s <= now` (all datetimes tz-aware UTC in, slots computed in `tz`), or `None`.

- [ ] **Step 1: Write the failing tests** `tests/unit/test_schedule_rules.py`: `test_empty_variables_mean_no_schedule`, `test_a_bad_cron_names_the_variable` (`"0 8 * * * *"`, `"every day"`, `"61 * * * *"`), `test_an_unknown_timezone_is_refused`, `test_the_next_slot_is_due_once_it_has_passed`, `test_three_days_of_downtime_give_one_slot_the_latest` (assert the returned slot is the latest, not the first), `test_nothing_is_due_before_the_slot`, `test_nothing_is_due_when_last_fired_is_in_the_future` (decision 7), `test_the_spring_forward_gap_fires_once` and `test_the_fall_back_hour_fires_once` (Europe/Amsterdam, `30 2 * * *` across 2026-03-29 and 2026-10-25; drive `due_slot` through a minute-by-minute sweep with `last_fired_at` updated to each returned slot and count the fires per day), `test_the_job_types_and_params_of_the_three_schedules` (decision 3).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_schedule_rules.py -x -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** settings and `schedule.py`. Use `croniter` with a tz-aware start time in `tz`; convert slots to UTC; reject anything but five fields explicitly (croniter accepts six).
- [ ] **Step 4: Run** the tests. Expected: PASS. Then `scripts/check` once.
- [ ] **Step 5: Commit** `feat: the schedule settings and the pure rules for which slot is due`.

### Task 2: The `ideas.pull` job

**Files:**
- Modify: `src/catcher/modules/worker/handlers_pipeline.py` (`handle_ideas_pull`, `parse_pull_params`), `src/catcher/modules/worker/app.py` (registry, `PARAM_CHECKS`)
- Test: `tests/integration/db/test_handler_ideas_pull.py`

**Interfaces:**
- Produces: `handle_ideas_pull(ctx: HandlerContext, job: Job) -> HandlerResult`; job type `ideas.pull` takes no params (any param is refused by `check_job`). It runs `git.pull(ctx.ideas, author=..., unattended=True)` like the publish handler does; a repo without a remote is a `Done` no-op saying so; a pull that fails (conflict, network) is `Fail(git's message)` after git's own rebase abort.
- Consumes: `catcher.core.git.pull`, `has_remote` (see how `handle_pipeline_publish` calls them near line 1082 and which author it passes).

- [ ] **Step 1: Write the failing tests** (real git, a local bare remote as in `test_handler_publish.py`): `test_pull_brings_a_new_capture_from_the_remote`, `test_pull_without_a_remote_is_a_noop_done`, `test_pull_keeps_a_dirty_working_copy` (autostash), `test_a_conflict_fails_the_job_and_leaves_no_rebase_in_progress`, `test_ideas_pull_refuses_params_and_is_in_the_registry` (`check_job("ideas.pull", {"x": 1})` raises; `jobs add ideas.pull` is accepted).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the handler and register it in `build_handlers` and `PARAM_CHECKS`.
- [ ] **Step 4: Run** `uv run pytest tests/integration/db/test_handler_ideas_pull.py -x -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: the ideas.pull job pulls idea-bucket`.

### Task 3: The `Scheduler` (strict review)

**Files:**
- Create: `src/catcher/modules/scheduler/loop.py`
- Test: `tests/integration/db/test_scheduler.py`

**Interfaces:**
- Consumes: Task 1 (`ScheduleSpec`, `due_slot`, `parse_schedules`), `queue.enqueue(session, type=..., now=..., params=..., dedupe_key=...) -> (job, created)`, the `Schedule` model (`name`, `last_fired_at`), `session_scope(engine)`.
- Produces:
  - `class Scheduler: def __init__(self, engine: Engine, specs: Sequence[ScheduleSpec], tz: ZoneInfo, clock: Callable[[], datetime])`
  - `def tick(self) -> list[str]`: for each spec, in one transaction: `SELECT ... FOR UPDATE` its `schedules` row (create it with `last_fired_at = now` when missing, firing nothing: decision 2); if `due_slot(...)` is a slot, enqueue the job with `dedupe_key=f"schedule:{name}"` and set `last_fired_at = now`; returns the names that queued a job. A database error on one spec is logged and does not stop the others.
  - `def run_forever(self, stop: threading.Event, tick_s: float) -> None`: tick, then `stop.wait(tick_s)`; any exception in a tick is logged (once per failure streak) and the loop goes on; returns when `stop` is set.

- [ ] **Step 1: Write the failing tests** (real Postgres, fake clock): `test_a_new_schedule_fires_nothing_and_stores_now`, `test_it_fires_on_the_slot_and_stores_last_fired` (job type and params per decision 3), `test_the_pull_fires_more_often_than_the_publish` (`*/30` vs `0 6,12,18,23`, run a simulated day tick by tick and count), `test_downtime_fires_once_and_a_second_tick_fires_nothing`, `test_a_running_previous_job_consumes_the_slot_without_a_second_job`, `test_two_schedulers_ticking_together_queue_one_job` (two `Scheduler` objects on one database, one thread each, same minute), `test_a_database_error_in_one_schedule_does_not_stop_the_others`, `test_run_forever_survives_a_failing_tick_and_stops_on_the_event`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** `Scheduler`. The row lock is what makes two schedulers safe; keep enqueue and the `last_fired_at` update in the same transaction.
- [ ] **Step 4: Run** `uv run pytest tests/integration/db/test_scheduler.py -x -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: the scheduler queues a job when a slot is due and runs a missed slot once`.

### Task 4: `catcher worker` runs the scheduler, `catcher schedules` shows it

**Files:**
- Modify: `src/catcher/cli.py`
- Test: `tests/integration/db/test_worker_cli.py` (extend), `tests/integration/db/test_schedules_cli.py` (new)

**Interfaces:**
- Consumes: Task 1 and 3.
- Produces: in `worker` (not `--once`): parse the schedules **before** taking the lock (`ValueError` -> message to stderr, exit 2, nothing started); after the lock, start a daemon-free thread running `Scheduler.run_forever(stop, settings.schedule_tick_s)` with its own use of `ctx.engine`, join it when the worker stops; log one line per schedule at start (`schedule pipeline_run: "0 8,12,17,21 * * *" Europe/Amsterdam, next 08:00`). A new command `catcher schedules`: a table of name, cron, timezone, last fired, next due for the three schedules (an empty one shows `off`); read-only, needs the database; with `--once` workers start no scheduler.

- [ ] **Step 1: Write the failing tests:** `test_worker_refuses_a_bad_cron_before_the_lock` (exit 2, message names `SCHEDULE_PIPELINE_RUN`, lock free afterwards), `test_the_worker_starts_the_scheduler_and_stops_it_with_the_worker` (fake clock via a test seam like the existing worker tests use; a due slot becomes a queued job; stop event ends both threads), `test_worker_once_starts_no_scheduler`, `test_schedules_lists_the_three_with_last_fired_and_next_due`, `test_schedules_shows_off_for_an_empty_variable`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** in `cli.py`; keep the thread's exceptions inside `Scheduler.run_forever` (Task 3) so it never kills the worker.
- [ ] **Step 4: Run** the two files. Expected: PASS.
- [ ] **Step 5: Commit** `feat: catcher worker runs the scheduler and catcher schedules shows it`.

### Task 5: The manual `catcher publish`

**Files:**
- Modify: `src/catcher/modules/worker/runner.py` (`publish_command`), `src/catcher/cli.py` (`publish` command)
- Test: `tests/integration/db/test_publish_command.py`

**Interfaces:**
- Consumes: `run_command`'s helpers in `runner.py` (`_enqueue`, `_drain`, the lock and services setup, the refusal over an earlier run's jobs, Ctrl-C handling): reuse them, do not copy.
- Produces: `publish_command(settings, *, ideas, docs, push: bool, ...) -> RunOutcome`-like result with `committed`, `pushed`, `exit_code`; CLI `catcher publish [--push] [--ideas PATH] [--docs PATH]`: queues `pipeline.publish` `{"push": push, "pull": push}` under the lock, drains, prints what was committed and pushed. Exit codes as `run pipeline` (0 ok, 1 a job failed or the lock was lost, 2 refused: worker or run running, database unreachable, an earlier `pipeline.run` or `pipeline.publish` still queued). Must not queue a `pipeline.run`.

- [ ] **Step 1: Write the failing tests:** `test_publish_commits_both_repos_and_does_not_push_by_default`, `test_publish_push_pushes_to_the_remotes`, `test_publish_with_nothing_to_commit_says_so_and_exits_0`, `test_publish_refuses_while_a_worker_runs` (exit 2, nothing changed), `test_publish_refuses_over_an_earlier_queued_run`, `test_publish_queues_no_pipeline_run`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** `publish_command` by sharing the lock/refusal/drain code of `run_command` (extract a helper if that is the smallest change).
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: catcher publish commits and optionally pushes by hand`.

### Task 6: Docs and a real check (strict review)

**Files:**
- Modify: `docs/idea-catcher-how-to-run-stage-b.md` (a "Scheduling" section: the variables, timezone, `catcher schedules`, `catcher publish`, what a missed slot does), `docs/idea-catcher-how-to-run.md` (a pointer), `docs/idea-catcher-service-architecture.md` (B6 built with the date, decisions above, open items), `README.md` status, `.env.example` if present.

- [ ] **Step 1: Verify for real** on a throwaway Postgres (a random-port `docker run`, never `catcher-db`; removed afterwards), a throwaway copy of the test repos in the scratchpad, `OPENAI_API_KEY=""`, `FREELLMAPI_URL=http://127.0.0.1:1/v1`, `YOUTUBE_OFFLINE=1`, never `CATCHER_ALLOW_NETWORK`: start `catcher worker` with `SCHEDULE_IDEAS_PULL="* * * * *"`, `SCHEDULE_PIPELINE_RUN="* * * * *"`, `SCHEDULE_PUBLISH="* * * * *"` against local bare remotes; watch `catcher schedules` and `catcher jobs list` for three minutes; stop the worker for two minutes and start it again: one catch-up per schedule.
- [ ] **Step 2: Write the docs** from what was verified and what tests prove.
- [ ] **Step 3: Run** `scripts/check` once. Expected: all checks pass.
- [ ] **Step 4: Commit** `docs: Stage B6 built`.

## Self-review

- **Spec coverage:** three variables, explicit timezone, 30 s loop, missed slot once, `last_fired_at` table, dedupe rule, manual `catcher publish`, `retry_deferred` on the scheduled run (Tasks 1, 3, 4, 5). The "pull idea-bucket" job did not exist and is Task 2.
- **Types:** `ScheduleSpec`, `due_slot`, `Scheduler.tick/run_forever` are defined in Tasks 1 and 3 and used with the same names in Task 4.
- **Review Focus:** DST (Task 1), downtime and double tick (Task 3), dedupe consumption (Task 3), bad cron/timezone (Tasks 1, 4), pull no-remote/dirty/conflict (Task 2), database error in a tick (Task 3).
- **Open question for the user:** the seven decisions at the top; the most likely to be changed are the timezone default and whether `catcher publish` should push by default.
