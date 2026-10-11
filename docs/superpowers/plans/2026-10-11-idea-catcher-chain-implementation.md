# Idea Catcher: the Chain (pull → run → publish → repeat) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Only `ideas.pull` is scheduled (once an hour). Every other job is queued by the job before it, so pull, run and publish always happen in order, and the cycle repeats until nothing is left in the inbox. The scheduler, the API and the CLI all use this one logic.

**Architecture:** A new module `catcher/modules/worker/chain.py` holds a pure decision function (job type, outcome, facts → the job to queue, or nothing) and a thin wrapper that reads the facts from Postgres and the inbox and calls `queue.enqueue` with a `dedupe_key`. `Worker.run_once` calls the wrapper once, right after `run_job`. The handlers do not change.

**Design:** `docs/superpowers/plans/2026-10-11-idea-catcher-chain-pull-run-publish.md` (read it first: the rule table, the decisions, the reasons).

**Tech Stack:** as Stage B: Python 3.12, SQLAlchemy 2 (sync) + psycopg 3, Typer, pytest + testcontainers (real Postgres, fake clock).

## Global Constraints

- No live LLM or YouTube call in any test; the network guard stays on; db tests use testcontainers, never the user's `catcher-db`. Use the fake profile and fixtures.
- Agents commit locally, never push (the user pushes). Never touch `tests/data`, `tmp/ic`, `~/.catcher`, `.env`.
- Edit AI instructions only in `.github/` (see CLAUDE.md); this plan touches none.
- One active chain job per key (`chain:run`, `chain:publish`) via `dedupe_key`; the worker stays single.
- A run with `limit`, `only` or `requeue` does at most one run; a `dry_run` queues nothing.
- Deferred documents are retried only by the first run of a cycle (`retry_deferred=true`); follow-up runs use `false`.

## Decisions made in this plan (defaults; the user can change them)

1. **`CHAIN_PUBLISH_PUSH`** (setting, default `true`) sets `pull` and `push` of a chained publish. Both test compose files set it to `false`.
2. **`follow_up` param on `pipeline.publish`** (bool, default `true`; `parse_publish_params` accepts it). A publish queued by the chain gets `follow_up=false` when the latest finished `pipeline.run` had `limit`, `only` or `requeue`. A publish queued by hand or by the API defaults to `true`.
3. **Which run is "the latest":** the most recent `pipeline.run` job by `created_at` that is not a `dry_run`. Document jobs carry no run params, so the chain reads them from there.
4. **"Unpublished work"** (used by the `ideas.pull` self-healing row): an item in `ready` or `published` since the last `succeeded` publish, or a repo ahead of its upstream. Pick the cheaper check that works on the test data and record it in the module docstring.
5. **The two removed schedules** (`SCHEDULE_PIPELINE_RUN`, `SCHEDULE_PUBLISH`): ignored with one warning if set; never a startup failure.

## Review Focus

- A limited run (`{"limit": 3}`): publishes, then stops. It must never queue a second run (this costs money on the live profile).
- Two triggers at once (pull tick and API run): one `pipeline.run`, one `pipeline.publish` (dedupe), no second cycle.
- A document job delayed by the YouTube gate or a `deferred` item: it must not hold the publish back; the delayed job's end must queue the next publish.
- A failed `ideas.pull`, a failed `llm.reason`, a failed publish: the cycle ends or continues exactly as the design table says; no endless loop.
- `retry_deferred`: `true` only on the run queued by `ideas.pull`; a down LLM must not cause a retry loop inside one cycle.
- The CLI `catcher run pipeline`: same result as before for the same data (the parity tests), but with no explicit publish step of its own.

## Task 1: the setting, and the two schedules go

**Files:** `src/catcher/core/config.py`, `src/catcher/modules/scheduler/schedule.py`, `.env.example`, `compose.test.yaml`, `compose.live-test.yaml`, `compose.local-test.yaml`, `README.md`, `tests/unit/test_schedule_rules.py`, `tests/integration/db/test_scheduler.py`, `tests/integration/db/test_schedules_cli.py`, `tests/integration/db/test_worker_cli.py`, `tests/unit/test_config.py`.

- [ ] Add `chain_publish_push: bool = True` (`CHAIN_PUBLISH_PUSH`) to `Settings`.
- [ ] Remove `schedule_pipeline_run` and `schedule_publish` and their rows in `SCHEDULE_NAMES` and `_JOBS`; only `ideas_pull` is left. If the old variables are set in the environment, the worker logs one warning that they are ignored.
- [ ] Update the existing schedule tests to the single schedule; add a test for the warning.
- [ ] Set `CHAIN_PUBLISH_PUSH: "false"` in both test compose files; document it in `.env.example`; set the default cron in the docs to `0 * * * *`.
- [ ] Run `uv run pytest tests/unit` and the scheduler db tests. Commit.

## Task 2: the decision function (pure)

**Files:** create `src/catcher/modules/worker/chain.py`, `tests/unit/test_chain_rules.py`.

- [ ] Write the failing unit tests, one per row of the design table, plus: `limit`/`only`/`requeue` → publish with `follow_up=false` and no follow-up run; `dry_run` → nothing; a document job delayed (`run_after` in the future) does not count as "can run now"; a failed `llm.reason` still counts as ended; a failed `ideas.pull` → nothing.
- [ ] Implement `decide(job_type, outcome, params, facts) -> Next | None` with a small frozen dataclass `Facts` (inbox_empty, deferred_due, document_jobs_runnable, run_active, unpublished_work, latest_run_limited, chain_publish_push) and `Next(type, params, dedupe_key)`. No database, no IO.
- [ ] Run the new tests until green. Commit.

## Task 3: the facts and the enqueue wrapper

**Files:** `src/catcher/modules/worker/chain.py`, create `tests/integration/db/test_chain_facts.py`.

- [ ] Write failing db tests (testcontainers, fake clock, fixture inbox): each fact is read correctly (inbox empty or not, runnable document jobs vs delayed ones, a run active, latest non-dry-run run limited or not).
- [ ] Implement `read_facts(session, ctx)` and `queue_next(session, ctx, job, outcome)`: read facts, call `decide`, `enqueue(..., dedupe_key=...)`. A dedupe hit is not an error. Do not commit: the caller's session does.
- [ ] Test that two calls at once give one active job per key. Commit.

## Task 4: hook it into the worker

**Files:** `src/catcher/modules/worker/loop.py`, `src/catcher/modules/worker/handlers_pipeline.py` (`parse_publish_params` accepts `follow_up`), `tests/integration/db/test_worker_loop.py`, create `tests/integration/db/test_chain_end_to_end.py`.

- [ ] Write the failing end-to-end tests with the fake profile: pull → run → publish → no follow-up (inbox empty); a second batch arriving during the run → exactly one follow-up cycle, then stop; `{"limit": 3}` → run, publish, stop (the rest stays in the inbox); `dry_run` → nothing queued; a failed pull → nothing queued; a failed publish with files left → a follow-up run.
- [ ] In `Worker.run_once`, after `run_job` (and after `fail_item_of`), call `queue_next` in its own committed session. A chain error is logged and never stops the worker (like `reap_safely`).
- [ ] Add `follow_up` to `parse_publish_params`; the publish handler returns it in its result, nothing else changes.
- [ ] Run the full db tests for the worker. Commit.

## Task 5: the CLI uses the same logic

This is the riskiest task. Do it on its own and review it separately.

**Files:** `src/catcher/modules/worker/runner.py`, `tests/integration/db/test_run_pipeline_cli.py`, `tests/integration/db/test_run_on_worker.py`, `tests/integration/db/test_run_report.py`.

- [ ] Before changing anything, run the three test files and note which pass (the parity baseline).
- [ ] Remove the explicit "queue and run `pipeline.publish`" step of `run pipeline`; drain the worker until nothing is due (the chain hook queues the publish and the follow-ups). Keep `wait_youtube_s`, the lock, Ctrl-C handling and the report.
- [ ] Make `--push` set `push` of the chained publish for that command (it overrides `CHAIN_PUBLISH_PUSH`); say so in the command's help.
- [ ] All three files still pass with the same results; add tests for `--push` and for the exit codes. Commit.

## Task 6: docs, recipe, smoke test

**Files:** `docs/idea-catcher-how-to-run-stage-b.md`, `docs/idea-catcher-how-to-run.md`, `docs/idea-catcher-service-architecture.md`, `docs/idea-catcher-first-local-test.md`, `tests/manual/catcher-recipe.http`, `scripts/compose-smoke`.

- [ ] Rewrite the scheduling sections: one schedule, the chain, the table, `CHAIN_PUBLISH_PUSH`, and what the removed variables do now (ignored, with a warning).
- [ ] Recipe: after requests 3, 4 and 5 the chain queues a publish; explain it where the jobs are listed (requests 9 and 11a to 11e). Note that request 5 with `limit` ends after one run.
- [ ] Run `bash scripts/compose-smoke` (about 8 minutes, needs Docker, no GitHub, LLM or YouTube) and record the result in the architecture doc, like the earlier stages.
- [ ] Run the whole test suite. Report any failure with its output. Commit.
