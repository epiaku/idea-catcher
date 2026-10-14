# Backfill API (docs backfill and channel processing) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start the YouTube backfill over HTTP as well as from the CLI: one endpoint backfills the YouTube links found in the `epiaku-docs` pages, one endpoint processes a YouTube channel or playlist, and a read endpoint shows the backlog.

**Architecture:** The API container has no repos, no YouTube access and no LLM key (its environment is `DATABASE_URL`, `API_KEYS`, `LOG_LEVEL` only), so both POST endpoints only **enqueue a job** and answer, exactly like `POST /pipeline/runs`. Two new job types, `backfill.docs` and `backfill.channel`, run in the worker, which has the repos and the YouTube gate. The logic is shared with the CLI by moving it from `cli.py` into `modules/backfill/service.py`. The chain starts a `pipeline.run` after a backfill job released clip notes, so released videos are processed without a second call.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2, Postgres 17 (real-Postgres tests, marker `db`), Typer, pytest.

**Spec:** this conversation (2026-10-14); background in `docs/idea-catcher-service-architecture.md` (Stage B8, Stage C, "chain") and `docs/idea-catcher-how-to-run-stage-b.md#backfill`.

## Global Constraints

- **No live calls in tests (user rule):** no YouTube, no LLM, no network. The channel extractor is a fake passed in; `catcher youtube import --channel ...` is never run by an agent.
- **The API never does the work.** A POST inserts a job and answers `202`, or `200` with the existing job's id (`existing: true`), same as `routes_runs.py`.
- **Scopes:** POSTs need `run`, GETs need `read`. No new scope.
- **One YouTube call at a time, through the gate.** A channel listing uses `YoutubeAccess.call(..., wait=False)`; a closed gate is a `Defer`, never a sleep. The claim must skip a `backfill.channel` job while the gate is closed (`JOB_RESOURCES`).
- **Backfill is low priority:** `backfill.channel` jobs are queued at `settings.backfill_priority` (default `-10`).
- **Listing size rule is unchanged:** `ceil(N/30) x YOUTUBE_REQUEST_DELAY_S <= YOUTUBE_MIN_GAP_S / 2`; `max_videos` is 1 to 100 (`MAX_CHANNEL_VIDEOS`), default 50 (`DEFAULT_CHANNEL_VIDEOS`).
- **The rolling 24 h cap is unchanged:** `daily_allowance(session, limit, now)` counts every release of the last 24 hours, whoever released.
- **Docs are English, plain, no em dashes** (project style). Edit `.github/` never; this plan touches `src/`, `tests/`, `docs/` only.
- **Commits:** one per task, plain `git add <paths>` then `git commit` (no explicit paths on `git commit`, see the B7 open item on `GIT_*` variables). Commit only when the user says so.

## Review Focus

- Two POSTs at the same moment for the same channel make one job (dedupe key), the second gets `200`.
- A `backfill.channel` job whose gate is closed stays `queued` with a future `run_after` and is not `failed`.
- A block during the listing opens the breaker and the job ends `failed` with the block message; the channels listed before it stay in the backlog.
- A docs repo without a remote, or a pull that fails, does not lose a scan of the files already on disk when `pull` is false; with `pull` true a git error is a `Fail` that names git's message.
- `release: 0` and a missing `release` both release nothing; a `release` above the unused allowance releases only the allowance.
- A channel URL that is a video, a playlist-less `/playlist`, or another host is `422` at the API, not a failed job.
- A request body with an unknown field, a string for `max_videos`, or more than 10 channels is `422`.
- After a backfill job released notes, exactly one `pipeline.run` is queued (not one per job) and nothing when the inbox is empty.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/catcher/modules/backfill/service.py` (new) | Shared logic of the CLI and the handlers: store a channel listing, release within the cap, size check of a listing |
| `src/catcher/modules/worker/handlers_backfill.py` (new) | The two job handlers and their param parsers |
| `src/catcher/modules/worker/app.py` | Register the handlers, `PARAM_CHECKS`, `JOB_RESOURCES` |
| `src/catcher/modules/worker/chain.py` | A succeeded backfill job queues a `pipeline.run` when the inbox has files |
| `src/catcher/api/routes_backfill.py` (new) | `POST /backfill/docs`, `POST /backfill/channels`, `GET /backfill` |
| `src/catcher/api/schemas.py` | `BackfillCountsOut`, `BackfillVideoOut` |
| `src/catcher/api/app.py` | Include the router |
| `src/catcher/cli.py` | `youtube import` calls `service.py` instead of its own copies |
| `tests/unit/`, `tests/integration/db/` | Tests per task |
| `docs/`, `tests/manual/catcher-api.http` | How-to, architecture, hand-test requests |

## API contract (decided here, used by all tasks)

```
POST /api/v1/backfill/docs        scope run
  body {"dry_run": bool=false, "pull": bool=true, "release": int>=0 | null}
  202 {"job_id": str, "existing": false}  |  200 {"job_id": str, "existing": true}

POST /api/v1/backfill/channels    scope run
  body {"urls": [str] (1..10), "max_videos": int 1..100 = 50, "release": int>=0 | null}
  202/200 {"jobs": [{"url": str, "job_id": str, "existing": bool}]}   (200 only if every job existed)
  422 for a URL that is not a channel or playlist, with the offending URL in detail

GET /api/v1/backfill?status=pending|released&source=docs|channel&limit=50&offset=0     scope read
  200 {"counts": {"pending": int, "released": int},
       "videos": Page[BackfillVideoOut]}
BackfillVideoOut: video_id, source, found_in, status, found_at, released_at | null
```

Job params and results (what the handlers read and store):

```
backfill.docs     params {"dry_run": bool, "pull": bool, "release": int | None}
                  result {"dry_run", "files", "found", "known", "new", "pending", "unreadable",
                          "released": [ids], "repaired": [ids], "still_pending": int, "allowance": int | None}
backfill.channel  params {"url": str, "max_videos": int, "release": int | None}
                  result {"url", "listed", "known", "new", "released": [ids], "repaired": [ids],
                          "still_pending": int}
```

---

### Task 1: Move the shared backfill logic out of the CLI

**Files:**
- Create: `src/catcher/modules/backfill/service.py`
- Modify: `src/catcher/cli.py` (`_check_the_listing_fits_the_gap`, `_list_channels` store step, `_release_backlog`)
- Test: `tests/unit/test_backfill_service.py`, `tests/integration/db/test_backfill_service.py`

**Interfaces:**
- Produces:
  - `listing_fit(max_videos: int, gap_s: float, delay_s: float) -> str | None` returns `None` when the listing fits half the gap, else the same refusal text the CLI prints today (the "use at most --max-videos N" and "raise the gap" messages, without the trailing "(nothing was done)").
  - `store_listing(session: Session, ids: list[str], url: str, ideas: Path, docs: Path, now: datetime) -> tuple[int, int]` returns `(already_known, added)`; source is `"channel"`, `found_in` is `url`.
  - `release_within(session: Session, ideas: Path, cap: int, now: datetime) -> tuple[int, ReleaseResult]` returns `(allowance, result)`; allowance `0` releases nothing.
- Consumes: `backfill.channel.listing_seconds/largest_listing`, `backfill.scan.known_ids`, `backfill.store.add_pending/counts`, `backfill.release.daily_allowance/release`.

- [ ] **Step 1: Write the failing tests**
  - Unit: `test_listing_fit_none_when_it_fits` (`listing_fit(30, 120, 10) is None`), `test_listing_fit_names_the_largest_size` (`listing_fit(100, 120, 20)` contains `"--max-videos 60"`... compute from `largest_listing(60, 20) == 90`; assert the number in the text equals that), `test_listing_fit_says_raise_the_gap_when_not_even_one_page_fits` (`listing_fit(30, 10, 20)` contains `"raise the gap"`).
  - DB: `test_store_listing_skips_known_and_stores_the_rest` (one id is a `job_items.doc_id`, two are new: returns `(1, 2)`, rows have `source == "channel"` and `found_in == url`), `test_release_within_respects_the_rolling_cap` (3 released an hour ago, cap 5, 10 pending: allowance 2, two notes written under `inbox/clippings/`), `test_release_within_zero_cap_releases_nothing`.
- [ ] **Step 2: Run to verify they fail:** `uv run pytest tests/unit/test_backfill_service.py tests/integration/db/test_backfill_service.py -q` fails on the missing module.
- [ ] **Step 3: Implement `service.py` with the three functions above**, moving the bodies from `cli.py` (the size check text, the `known_ids`/`add_pending` block of `_list_channels`, and the allowance/`release` core of `_release_backlog`). Printing stays in the CLI.
- [ ] **Step 4: Make `cli.py` call them.** `_check_the_listing_fits_the_gap` becomes `listing_fit` plus the existing `typer.echo(... "(nothing was done)")` and `Exit(2)`; `_list_channels` calls `store_listing`; `_release_backlog` calls `release_within` and keeps its output lines word for word.
- [ ] **Step 5: Run the whole backfill CLI test set:** `uv run pytest tests -q -k "backfill or youtube_import"` passes unchanged (no CLI test may need editing; if one does, the move changed behavior).
- [ ] **Step 6: Commit** `refactor: share the backfill logic between the CLI and the worker`.

---

### Task 2: `backfill.docs` job handler

**Files:**
- Create: `src/catcher/modules/worker/handlers_backfill.py`
- Modify: `src/catcher/modules/worker/app.py`
- Test: `tests/integration/db/test_handler_backfill_docs.py`

**Interfaces:**
- Consumes: `run_import(session, ideas, docs, now, *, dry_run) -> ImportResult` (`modules/backfill/importer.py`), `release_within` (Task 1), `GIT_LOCK`, `pull`, `has_remote`, `require_on_branch` as used by `handle_ideas_pull`.
- Produces:
  - `parse_docs_params(params: dict[str, Any]) -> DocsParams` (frozen dataclass `dry_run: bool=False, pull: bool=True, release: int | None=None`); raises `ValueError` naming the bad parameter for an unknown key, a non-bool flag, or a `release` that is not an int >= 0 (`bool` is not an int).
  - `handle_backfill_docs(ctx: HandlerContext, job: Job) -> HandlerResult`.
  - Registry name `"backfill.docs"`, and `PARAM_CHECKS["backfill.docs"] = parse_docs_params`.

- [ ] **Step 1: Write the failing tests** (use the harness of `test_handler_pull.py`/`test_handlers_pipeline*.py` for `ctx` and throwaway repos):
  - `test_params_reject_unknown_and_wrong_types` (`{"x": 1}`, `{"release": "5"}`, `{"release": True}`, `{"pull": "yes"}` each raise `ValueError`).
  - `test_dry_run_writes_no_row_no_file` (result `dry_run` true, `new` equals the links without a page, table and inbox unchanged).
  - `test_stores_new_links_and_skips_known` (docs repo with 3 links, one already a page: result `new == 2`, `known == 1`).
  - `test_release_writes_clip_notes_within_the_cap` (`release=1`, 2 pending: one note `inbox/clippings/youtube source - <id>.md` with `backfill: true`, `still_pending == 1`).
  - `test_no_release_key_releases_nothing`.
  - `test_pull_true_without_remote_still_scans` and `test_pull_failure_is_fail_with_gits_message` (docs repo with a remote that cannot be reached: `Fail`, message contains git's text, nothing stored).
- [ ] **Step 2: Run to verify they fail:** `uv run pytest tests/integration/db/test_handler_backfill_docs.py -q`.
- [ ] **Step 3: Implement `handle_backfill_docs`.** Order: parse (`Fail(str(e))` on `ValueError`); if `pull` and not `dry_run`, under `GIT_LOCK` pull `ctx.docs` (skip with no remote, `Fail` on `GitError`; the docs repo is only pulled, never committed here); `run_import` in a `session_scope`; if `release` is not None and not `dry_run`, `release_within` in the same scope; return `Done(result)` with the shape in the contract above (`allowance` is `None` when no release was asked). Log one line per step with the job id like the other handlers.
- [ ] **Step 4: Register it** in `build_handlers()` and `PARAM_CHECKS` in `app.py`.
- [ ] **Step 5: Run the tests** (expected PASS), then `uv run pytest tests/unit tests/component -q` (no registry test may break; update the test that lists the handler names if there is one).
- [ ] **Step 6: Commit** `feat: backfill.docs job`.

---

### Task 3: `backfill.channel` job handler

**Files:**
- Modify: `src/catcher/modules/worker/handlers_backfill.py`, `src/catcher/modules/worker/app.py`
- Test: `tests/integration/db/test_handler_backfill_channel.py`

**Interfaces:**
- Consumes: `is_channel_url`, `listing_url`, `list_channel`, `ChannelListingError`, `build_extractor`, `MAX_CHANNEL_VIDEOS`, `DEFAULT_CHANNEL_VIDEOS` (find where the CLI imports the two constants from and import from there), `YoutubeAccess.call`, `FactsDeferred`, `FactsUnavailable`, `GateUnavailable`, `listing_fit`, `store_listing`, `release_within`, `Services`/`ctx.services.youtube` (the access object `handle_youtube_fetch` uses through `get_facts`; use the same attribute).
- Produces:
  - `parse_channel_params(params: dict[str, Any]) -> ChannelParams` (`url: str, max_videos: int = DEFAULT_CHANNEL_VIDEOS, release: int | None = None`); `ValueError` when `url` is missing or not `is_channel_url`, `max_videos` outside 1..`MAX_CHANNEL_VIDEOS`, `release` not an int >= 0, or any unknown key.
  - `handle_backfill_channel(ctx, job) -> HandlerResult`; an injectable extractor seam: `EXTRACTOR_FACTORY: Callable[[float], Extractor] = build_extractor` module attribute that tests replace (production never assigns it).
  - `"backfill.channel"` in the registry, `PARAM_CHECKS`, and `JOB_RESOURCES = {..., "backfill.channel": YOUTUBE_RESOURCE}`.

- [ ] **Step 1: Write the failing tests** with a fake extractor returning flat entries:
  - Params: `test_channel_params_reject_a_video_url_and_a_big_max_videos`.
  - `test_lists_and_stores_unknown_videos` (result `listed 5, known 2, new 3`, rows `source == "channel"`).
  - `test_size_over_the_gap_rule_is_fail_with_the_largest_size` (settings gap 120, delay 20, `max_videos=100`: `Fail`, no extractor call).
  - `test_closed_gate_defers_the_job` (gate in a gap: result is `Defer` with `run_after` equal to the gate's next slot, extractor not called, nothing stored).
  - `test_block_during_listing_fails_and_opens_the_breaker` (extractor raises an error `is_block_error` accepts: `Fail`, gate `snapshot` shows blocked).
  - `test_offline_is_fail` (`YOUTUBE_OFFLINE` on: `Fail("YOUTUBE_OFFLINE is on ...")`, no call).
  - `test_release_after_listing_respects_the_cap`.
  - `test_claim_skips_the_job_while_the_gate_is_closed` (enqueue with `resource=YOUTUBE_RESOURCE` through the same helper the API will use, close the gate row, `claim` returns nothing; open it, `claim` returns the job).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement `handle_backfill_channel`.** Order: parse; offline check (`Fail`); `listing_fit` against `ctx.settings.youtube_min_gap_s/youtube_request_delay_s` (`Fail` with its text); `access.call(lambda: list_channel(listing_url(url), max_videos=..., extractor=EXTRACTOR_FACTORY(delay)), wait=False)`; map `FactsDeferred` to `Defer(run_after=<from e.until, else now + DEFER_FALLBACK_S like _fetch>, reason=str(e))`, and `FactsUnavailable`, `ChannelListingError` to `Fail(str(e))`; then `store_listing` and optional `release_within` in one `session_scope`; `Done(result)` per the contract.
- [ ] **Step 4: Register** the handler, the param check and `JOB_RESOURCES`.
- [ ] **Step 5: Run the tests (PASS)**, then `uv run pytest tests -q -m "not db"` to catch registry tests.
- [ ] **Step 6: Commit** `feat: backfill.channel job`.

---

### Task 4: The chain starts a run after a backfill job released notes

**Files:**
- Modify: `src/catcher/modules/worker/chain.py` (`decide`, `_needed`, `end_of_chain` if it needs a line)
- Test: `tests/unit/test_chain_decide.py` (add cases; find the existing file with `grep -l "decide(" tests -r`)

**Interfaces:**
- Consumes: `Facts`, `_run(retry_deferred: bool, limit=None) -> Next`, `RUN_KEY`.
- Produces: `BACKFILL_JOBS = ("backfill.docs", "backfill.channel")` in `chain.py`; `decide` returns `_run(retry_deferred=False)` for a succeeded backfill job when `not facts.inbox_empty and not facts.run_active`, else `None`; `_needed` returns `{"inbox_empty", "run_active"}` for these types.

- [ ] **Step 1: Write the failing tests:**
  - `test_backfill_succeeded_with_files_in_the_inbox_queues_a_run`.
  - `test_backfill_succeeded_with_empty_inbox_queues_nothing`.
  - `test_backfill_succeeded_while_a_run_is_active_queues_nothing`.
  - `test_backfill_failed_and_deferred_queue_nothing`.
  - `test_the_run_key_dedupes_two_backfill_jobs` (DB test: two succeeded backfill jobs in a row leave one active `chain:run` job).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the `decide` branch and `_needed` entry. The follow-on publish needs no change: the run's end already queues it (`decide` for `RUN`).
- [ ] **Step 4: Run** `uv run pytest tests -q -k chain` (PASS, nothing else changed).
- [ ] **Step 5: Commit** `feat: chain a run after a backfill release`.

---

### Task 5: `POST /backfill/docs` and `POST /backfill/channels`

**Files:**
- Create: `src/catcher/api/routes_backfill.py`
- Modify: `src/catcher/api/app.py` (include the router)
- Test: `tests/integration/db/test_api_backfill.py`

**Interfaces:**
- Consumes: `require("run")`, `get_session`, `Enqueued`, `_answer`, `_checked`, `_once` and `active_job` from `routes_runs.py` (import them; do not copy), `enqueue(... priority=, resource=, dedupe_key=)`, `YOUTUBE_RESOURCE`, `check_job`.
- Produces:
  - `DocsRequest(dry_run: StrictBool=False, pull: StrictBool=True, release: Annotated[StrictInt | None, Field(ge=0, le=2**31-1)]=None)`, `extra="forbid"`.
  - `ChannelsRequest(urls: Annotated[list[StrictStr], Field(min_length=1, max_length=10)], max_videos: Annotated[StrictInt, Field(ge=1, le=100)]=50, release: ... =None)`, `extra="forbid"`.
  - `ChannelEnqueued(url: str, job_id: str, existing: bool)` and `ChannelsEnqueued(jobs: list[ChannelEnqueued])`.
  - Dedupe: docs by `active_job(session, "backfill.docs")` (params may differ), key `api:backfill.docs`; a dry run is its own job type of the same name with `dry_run` true and is never the existing job of a real one (match on params like the preview does). Channels: key `api:backfill.channel:<sha256(url)[:32]>`, existing job found by `Job.params["url"].astext == url` in a queued or running job.

- [ ] **Step 1: Write the failing tests** (client fixture as in `test_api_runs.py`, with `READER`/`RUNNER` keys):
  - Auth: `test_reader_key_gets_403_on_both_posts`, `test_no_key_is_401`.
  - Docs: `test_post_docs_enqueues_backfill_docs_with_202`, `test_second_post_docs_is_200_with_the_same_job_id`, `test_dry_run_is_its_own_job`, `test_unknown_field_is_422`, `test_negative_release_is_422`, `test_string_release_is_422`.
  - Channels: `test_post_channels_enqueues_one_job_per_url_at_backfill_priority_and_with_the_youtube_resource` (assert `priority == -10`, `resource == "youtube"`), `test_same_url_twice_returns_the_existing_job_with_200`, `test_a_video_url_is_422_and_names_the_url_and_nothing_is_enqueued`, `test_more_than_10_urls_is_422`, `test_max_videos_101_is_422`, `test_two_posts_at_once_make_one_job` (thread pool like the run test).
  - `test_openapi_snapshot_lists_the_new_paths` if the repo has an OpenAPI snapshot test (`grep -rl openapi tests`); update the snapshot.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement the router** (`prefix="/api/v1"`, `dependencies=[Depends(require("run"))]`). Validate each URL with `is_channel_url` before enqueueing anything (all-or-nothing) and `_checked("backfill.channel", params)` so the handler's own parser has the last word. Include the router in `create_app`.
- [ ] **Step 4: Run the tests (PASS)** and `uv run pytest tests/integration/db -q -k api`.
- [ ] **Step 5: Commit** `feat: backfill trigger endpoints`.

---

### Task 6: `GET /backfill` (counts and the backlog)

**Files:**
- Modify: `src/catcher/api/routes_backfill.py`, `src/catcher/api/schemas.py`
- Test: `tests/integration/db/test_api_backfill_read.py`

**Interfaces:**
- Consumes: `require("read")` (a second router in the same file with its own dependency, as `routes_youtube.py` does), `backfill.store.counts(session) -> dict[str, int]`, the model class `BackfillVideo` (find it with `grep -n "class BackfillVideo" -r src`), `Page[T]`.
- Produces: `BackfillVideoOut(video_id, source, found_in, status, found_at, released_at)`, `BackfillOut(counts: dict[str, int], videos: Page[BackfillVideoOut])`; filters `status`, `source` (`Literal`), `limit` 1..200 default 50, `offset`; order oldest `found_at` first then `video_id` (the release order).

- [ ] **Step 1: Write the failing tests:** `test_counts_and_first_page`, `test_status_filter_pending_and_released`, `test_source_filter`, `test_order_is_the_release_order`, `test_bad_status_is_422`, `test_limit_201_is_422`, `test_runner_only_key_gets_403` (a key with only `run`), `test_empty_backlog_returns_zero_counts_and_an_empty_page`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement the endpoint and schemas.** `counts` always has both keys (`pending`, `released`), `0` when empty.
- [ ] **Step 4: Run the tests (PASS).**
- [ ] **Step 5: Commit** `feat: GET /backfill`.

---

### Task 7: Docs, hand-test requests and the final check

**Files:**
- Modify: `docs/idea-catcher-how-to-run-api.md` (new endpoints, a curl example each, the status codes, remove "backfill" from anything that says the API cannot do it), `docs/idea-catcher-how-to-run-stage-b.md` (#backfill: "or over the API"; the job types in the job list), `docs/idea-catcher-service-architecture.md` (a "Built: backfill API" block with the decisions below and an "Open items" list), `tests/manual/catcher-api.http` (one request per endpoint, the channel one marked "calls YouTube, hand test only, small channel").
- Test: none new; run the whole suite.

- [ ] **Step 1: Write the doc changes.** Decisions to record: the API only enqueues (no repos in its container); the channel is one job per URL; `backfill.channel` runs at priority `-10` and waits on the gate resource; a backfill job that released notes makes the chain queue a run; `POST /backfill/docs {"release": N}` with nothing new simply releases (this is the daily release; no separate release endpoint); the rolling cap is shared with the CLI; not built: a schedule that calls it every day, cancel of a backfill job, a per-request progress stream (poll `GET /jobs/{id}`).
- [ ] **Step 2: Run the full gate:** `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q && uv run pytest tests/integration/db -q` all pass (the db tests need Docker; say so if Docker was not available instead of claiming a pass).
- [ ] **Step 3: Commit** `docs: backfill API`.
- [ ] **Step 4 (by the user, not an agent):** the live check. `docker compose up`, `POST /backfill/docs {"dry_run": true}`, read the job result, then `POST /backfill/docs {"release": 5}`, watch the chain run and `catcher youtube gate`; then one small channel with `max_videos: 20`. This is the first real YouTube listing, the same untested step as in the CLI how-to.

---

## Self-review

- **Coverage:** backfill from docs = Tasks 2, 5; channel = Tasks 3, 5; records = Task 6; processing after release = Task 4; shared CLI logic = Task 1; docs = Task 7.
- **Types:** `release_within -> tuple[int, ReleaseResult]` and `store_listing -> tuple[int, int]` are used the same way in Tasks 2 and 3; job param names (`dry_run`, `pull`, `release`, `url`, `max_videos`) are the same in the contract, handlers and routes.
- **Review Focus** lines map to tests in Tasks 3 (gate, block), 2 (pull), 5 (422, dedupe, race), 4 (chain), 2/3 (cap).
- **Open choices the user may want to change** (defaults chosen here): max 10 channels per request; `pull` of the docs repo defaults to true; no separate release endpoint; one job per channel; the channel job is lower priority than document jobs.
