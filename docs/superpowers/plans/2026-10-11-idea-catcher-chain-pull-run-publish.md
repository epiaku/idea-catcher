# Design: one schedule, then a chain (pull → run → publish → repeat)

**Written:** 2026-10-11
**Written for:** the owner, to approve before any code is written.
**Status:** agreed in conversation, not built.

## The idea

Today three independent schedules (`SCHEDULE_IDEAS_PULL`, `SCHEDULE_PIPELINE_RUN`, `SCHEDULE_PUBLISH`) have to be spaced
in the right order by hand. New: **only `ideas.pull` is scheduled.** Every other job is queued by the job before it, so
the order cannot go wrong, and the same logic holds for the scheduler, the API and the CLI.

```
SCHEDULE_IDEAS_PULL (cron)
  └─ ideas.pull ──► pipeline.run (retry_deferred=true) ──► youtube.fetch / llm.reason per document
                                                                   │ (the last one that can run now ends)
                                                                   ▼
                    ┌────────────────────── pipeline.publish (commit, pull, push) ◄──┘
                    │ inbox not empty after the publish?
                    ├─ yes ─► pipeline.run (retry_deferred=false) ─► … (same loop)
                    └─ no  ─► stop. The next ideas.pull tick starts again.
```

## The rule: "what next?"

One small module, `modules/worker/chain.py`: a **pure decision function** (job type, outcome, a few facts read from the
database and the inbox → the job to queue, or nothing) and a thin wrapper that reads the facts and calls `enqueue`.
The worker calls it in one place, `Worker.run_once`, right after `run_job` returns. The handlers stay as they are.

| A job just ended | Condition | Queue |
|---|---|---|
| `ideas.pull` succeeded | inbox not empty, or a `deferred`/`stuck` item is due | `pipeline.run` with `retry_deferred=true` |
| `ideas.pull` succeeded | inbox empty, nothing to retry, but unpublished work | `pipeline.publish` |
| `ideas.pull` failed | | nothing (the next tick tries again) |
| `pipeline.run`, `youtube.fetch` or `llm.reason` ended (any outcome) | no document job can run now, and no `pipeline.run` is active | `pipeline.publish` |
| `pipeline.publish` ended (any outcome) | inbox not empty | `pipeline.run` with `retry_deferred=false` |
| `pipeline.publish` ended | inbox empty | nothing: the cycle is over |

- **"Can run now"** means queued or running and due. A job delayed by the YouTube gate (`run_after` in the future) and a
  `deferred` item do **not** hold the publish back. When a delayed job finishes later, the same row of the table queues
  the next publish.
- **"Unless already running":** every chain job is queued with a `dedupe_key` (`chain:run`, `chain:publish`), so at most
  one is active. The worker is single, so a run never executes during a publish.
- **Failures:** a failed `llm.reason` still counts as ended, so the publish publishes what succeeded. A failed publish
  (its pull failed) still queues a follow-up run if the inbox is not empty; a failed `ideas.pull` stops the cycle.
- **Deferred documents** are retried only by the first run of a cycle (the one after `ideas.pull`). Follow-up runs use
  `retry_deferred=false`, so a down LLM or a used-up budget cannot cause a retry loop inside one cycle.
- **Self-healing:** `ideas.pull` is the entry point at every tick and picks the right step from the facts (table above).
  If the worker crashed between two steps, the next tick restarts the chain at the right place.

## Runs that must not continue the loop (decided)

A run started with `limit`, `only` or `requeue` (for example request 5 with `{"limit": 3}`) is a special case: **at most
one run.** It still ends in a publish, but that publish does **not** queue a follow-up run, even when files are left in
the inbox on purpose. Otherwise a normal run would process everything, and with the live profile that costs money.
A `dry_run` (preview) queues nothing at all. The decision function gets the run's params for this.

## What changes

- **Remove** `SCHEDULE_PIPELINE_RUN` and `SCHEDULE_PUBLISH`: settings, scheduler (`schedule.py`), compose files, docs
  and tests. If either is still set in `.env`, the worker logs one warning that it is ignored; it does not fail.
- **API** (`POST /pipeline/runs`, `/pipeline/publish`) and **`catcher jobs add`**: unchanged; the chain continues from
  whatever they queue, because the hook runs after every job.
- **CLI `catcher run pipeline`:** it queues `pipeline.run` and drains the worker, and the chain rule queues the publish.
  The explicit "queue and run `pipeline.publish`" step in `runner.py` goes away, so there is one logic for the CLI, the
  API and the scheduler. `wait_youtube_s` stays. This is the riskiest part: do it as its own step with the parity tests.
- **Chained publish and `push` (decided):** a new setting `CHAIN_PUBLISH_PUSH` (default `true`) sets `push` and `pull`
  of the publish the chain queues. The two test stacks (`compose.local-test.yaml`, `compose.live-test.yaml`) set it to
  `false`: the publish then only **commits locally** in the `tmp/ic` repos, and nothing leaves the machine. A publish
  queued by hand or via the API keeps the params it was given.
- **Recipe** (`catcher-recipe.http`): after request 3, 4 or 5 the chain also queues a `pipeline.publish`, which now
  succeeds locally (a commit in `tmp/ic`). Requests 11a to 11e will show it as `succeeded`.

## Tests (fixtures only: no LLM, no YouTube)

- Unit tests of the decision function: every row of the table, with `limit`/`only`/`requeue`, `dry_run`, delayed jobs
  and the failure cases.
- Integration tests on the test data with the fake profile: pull → run → publish → no follow-up; a second batch
  arriving during the first run → one follow-up cycle, then stop; two triggers at once → one active job per key.
- The existing run/publish tests and the compose smoke test keep passing.

## Decided settings

- `SCHEDULE_IDEAS_PULL` = once an hour (`0 * * * *`), also in the docs and the compose files.
- `CHAIN_PUBLISH_PUSH` = `true` in production, `false` in the test stacks.
