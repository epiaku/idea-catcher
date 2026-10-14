# Backfill: release endpoint and newest-first order Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) An API call that releases pending backfill videos of any source. (2) Within one channel listing, release the newest videos first.

**Architecture:** (1) A new job type `backfill.release` and `POST /api/v1/backfill/release`: no pull, no YouTube, it only calls `release_within(..., only=None)` in the worker, so the rolling 24 h cap still applies. The chain treats it like the other backfill jobs. (2) A channel listing stores its new videos with `found_at` offsets of one microsecond per listing position, so the existing release order (`found_at`, then `video_id`) follows the listing (newest first).

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2, Postgres 17 (marker `db`), pytest.

**Spec:** the two decisions of 2026-10-14 in this conversation ("1 yes, 2 yes" to the final-review questions); builds on `docs/superpowers/plans/2026-10-14-idea-catcher-backfill-release-by-default.md`.

## Decisions

- New endpoint `POST /api/v1/backfill/release` (scope `run`), body `{"limit": int 1..100 | null}`; omitted or null = the worker's `BACKFILL_DAILY_LIMIT` (the API cannot see it, so the key is omitted and the worker resolves it). Job type `backfill.release`, params `{"limit": N}` or `{}`.
- It releases the oldest pending videos of ANY source (`found_at`, then `video_id`), capped by the rolling 24 h cap (`limit` is the cap itself, like `release: N`: N minus everything released in the last 24 h). `limit` is not `0`/`false`: use no call at all to release nothing (422 for 0).
- Dedupe: one active `backfill.release` job at a time (`api:backfill.release`); a second POST gets `200` with the existing job, whatever its `limit`.
- Order: within one channel listing the newest video has the smallest `found_at`. Docs-scan rows keep the current tie-break by `video_id` (unchanged). Across calls, videos found earlier still release first.
- Result of `backfill.release`: `{"released": [ids], "repaired": [ids], "still_pending": int, "allowance": int}`.
- The chain queues a `pipeline.run` after a succeeded `backfill.release` exactly as after the other backfill jobs: only when `result["released"]` is non-empty, the inbox has files and no run is active.

## Global Constraints

- No live calls in tests: no YouTube, no LLM, no network. The release job never calls YouTube.
- The rolling 24 h cap (`daily_allowance`) applies to this endpoint, shared with the CLI and the other endpoints.
- API ceiling `limit <= 100` (`MAX_RELEASE`). Strict types (no coercion): `true`, `"5"`, `1.5`, `0`, `-1`, `101` are 422.
- Docs plain English, no em dashes; no `max_items`.
- Commit with `git add <paths>` then plain `git commit`; never stage the untracked plan files; never push; trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

- `backfill.release` with the cap used up releases nothing and still ends `succeeded`; it never queues a run in that case.
- A pending video of a docs scan AND of a channel are both released (any source), oldest first.
- Two POSTs at the same moment make one job (second gets `200`); `limit` 101, 0, `true`, a string are 422.
- A listing of 5 new videos stored with offsets releases in listing order when `release: 3` (the first 3 listed), even though all have the same `now`.
- Videos already pending from an earlier call keep their old `found_at` (the offset applies only to rows inserted by this call).
- The docs-scan release order is unchanged.

---

### Task 1: Newest first within a channel listing

**Files:**
- Modify: `src/catcher/modules/backfill/store.py` (`add_pending`), `src/catcher/modules/backfill/service.py` (`store_listing`)
- Test: `tests/integration/db/test_backfill_service.py` (and the store test file; find it with `grep -rl "add_pending" tests`)

**Interfaces:**
- Produces: `add_pending(session, found: Mapping[str, tuple[str, str]], now: datetime, *, ordered: bool = False) -> int`: with `ordered=True`, the i-th entry in iteration order (0-based) gets `found_at = now + timedelta(microseconds=i)`; default behavior unchanged (every row `now`). `store_listing` passes `ordered=True` and builds its `fresh` mapping in the order of `ids` (the listing order).

- [ ] **Step 1: Write the failing tests:** `test_add_pending_ordered_gives_each_row_a_later_found_at` (3 rows: strictly increasing `found_at` in insertion order), `test_add_pending_default_keeps_one_found_at` (all equal to `now`), `test_store_listing_keeps_the_listing_order` (ids `[Z, A, M]` all new: `store.pending(session)` returns `[Z, A, M]`, not alphabetical), `test_store_listing_skips_known_but_keeps_the_order_of_the_rest`, `test_release_within_after_a_listing_releases_the_first_listed` (5 listed, cap 3: the first 3 listed are released), and `test_docs_scan_order_is_unchanged` (docs rows still tie-break by `video_id`).
- [ ] **Step 2: Run to verify they fail:** `uv run pytest tests/integration/db -q -k "add_pending or store_listing or listing_order"`.
- [ ] **Step 3: Implement** the `ordered` parameter and the `store_listing` change; ids already in the table keep their old `found_at` (conflicts are ignored as today).
- [ ] **Step 4: Run** the new tests and `uv run pytest tests -q -k backfill` (PASS).
- [ ] **Step 5: Commit** `feat: release a channel listing newest first`.

---

### Task 2: The `backfill.release` job

**Files:**
- Modify: `src/catcher/modules/worker/handlers_backfill.py`, `src/catcher/modules/worker/app.py` (registry, `PARAM_CHECKS`), `src/catcher/modules/worker/chain.py` (`BACKFILL_JOBS`, `_needed`/facts for the new type)
- Test: `tests/integration/db/test_handler_backfill_release.py` (new), `tests/unit/test_chain_rules.py`, `tests/integration/db/test_chain_facts.py`

**Interfaces:**
- Consumes: `release_within(session, ideas, cap, now, only=None) -> tuple[int, ReleaseResult]`, `release_cap`-style resolution (`BACKFILL_DAILY_LIMIT` default).
- Produces: `parse_release_params(params: dict[str, Any]) -> ReleaseParams` (frozen dataclass `limit: int | None = None`); unknown key, a non-int/bool/negative/zero `limit` raise `ValueError` naming `limit`. `handle_backfill_release(ctx: HandlerContext, job: Job) -> HandlerResult`: cap = `limit` or `ctx.settings.backfill_daily_limit`; if the cap is 0 return `Done` with `released: []`, `allowance: 0` without calling `release_within`; else `release_within(session, ctx.ideas, cap, ctx.clock(), only=None)` in one `session_scope`; result `{"released", "repaired", "still_pending", "allowance"}`. Registered as `"backfill.release"`; `PARAM_CHECKS["backfill.release"] = parse_release_params`. The chain's backfill rule covers the new type (it reads `result["released"]` as for the other two).

- [ ] **Step 1: Write the failing tests:** parser (`{}` and `{"limit": 5}` accepted; `{"limit": 0}`, `-1`, `"5"`, `True`, `1.5`, `{"x": 1}` raise `ValueError`); handler: `test_releases_the_oldest_pending_of_any_source` (a docs row and a channel row pending, cap 5: both released, oldest first), `test_limit_is_the_cap`, `test_default_cap_is_the_worker_setting` (`backfill_daily_limit=2`, 3 pending: 2 released), `test_cap_used_up_releases_nothing_and_succeeds`, `test_backfill_daily_limit_zero_releases_nothing`, `test_result_keys`; chain: unit cases for `backfill.release` (released nothing -> none; released + inbox non-empty + no run active -> one run; failed/deferred -> none) and a DB test that `read_facts` reads `result["released"]` for the new type.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** handler, parser, registration, and the chain additions (reuse `BACKFILL_JOBS` and `_BACKFILL_FACTS`; do not duplicate the rule).
- [ ] **Step 4: Run** the new tests and `uv run pytest tests -q -k "backfill or chain or worker"` (PASS); update any registry-listing test that enumerates handler names.
- [ ] **Step 5: Commit** `feat: backfill.release job`.

---

### Task 3: `POST /api/v1/backfill/release`

**Files:**
- Modify: `src/catcher/api/routes_backfill.py`
- Test: `tests/integration/db/test_api_backfill.py` (add), or a new `tests/integration/db/test_api_backfill_release.py`

**Interfaces:**
- Consumes: `check_job("backfill.release", params)`, `_once`/`_answer`/`Enqueued`/`ACTIVE` as used by the other backfill routes.
- Produces: `ReleaseRequest(limit: Annotated[StrictInt, Field(ge=1, le=MAX_RELEASE)] | None = None)` with `extra="forbid"`; `POST /api/v1/backfill/release` behind `require("run")` returning `Enqueued` (`202`, or `200` with the existing job; dedupe key `api:backfill.release`, the existing job is any queued or running `backfill.release` whatever its `limit`). `limit` omitted or null adds no key to the job params.

- [ ] **Step 1: Write the failing tests:** `test_post_release_enqueues_backfill_release_with_202`, `test_limit_is_stored_when_given_and_absent_when_omitted_or_null`, `test_second_post_is_200_with_the_same_job_id_whatever_the_limit`, `test_two_posts_at_once_make_one_job` (thread pool like the other race tests), `test_limit_0_101_negative_true_string_float_are_422` (nothing enqueued), `test_unknown_field_is_422`, `test_reader_key_is_403_and_no_key_is_401`, `test_openapi_lists_the_release_path`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the model and route; document in the route docstring: releases the oldest pending of any source, the 24 h cap applies, leftovers stay pending.
- [ ] **Step 4: Run** `uv run pytest tests/integration/db -q -k api` (PASS).
- [ ] **Step 5: Commit** `feat: POST /backfill/release`.

---

### Task 4: Docs, requests and the final check

**Files:**
- Modify: `docs/idea-catcher-how-to-run-api.md`, `docs/idea-catcher-how-to-run-stage-b.md`, `docs/idea-catcher-service-architecture.md`, `docs/idea-catcher-youtube-backfill-explained.md`, `tests/manual/catcher-api.http`

- [ ] **Step 1: Update the docs and requests.** Add the `POST /api/v1/backfill/release` section (body, `202`/`200`, `422` cases, the cap, any source, oldest first, result keys) with a curl example, status-code table row and the `.http` request; add the `backfill.release` job type where the job types are listed; in the explainer, add it to the flows section (a third way to release: "release the backlog" with no listing or scan) and to the decisions table; replace the earlier "leftovers need another listing or the CLI" statements (explainer, API page, architecture decision 5 and "Not built") with the new endpoint; replace "release order within a listing is by video id" with "newest first within a listing; videos from earlier calls first; docs-scan rows tie-break by video id"; remove the "an API call that releases pending videos of any source" item from "Not built". Add a short "Built: release endpoint and listing order" note in the architecture doc.
- [ ] **Step 2: Run the full gate:** `uv run ruff check . && uv run pyright`, then `uv run pytest -q`, then `uv run pytest tests/integration/db -q` as separate commands (a chained run with `| tail` hung before). `ruff format --check` flags two untouched files (`docs/idea-catcher-pipeline.md`, `docs/superpowers/plans/2026-10-06-idea-catcher-stage-b5b-parity-table.md`); leave them.
- [ ] **Step 3: Commit** `docs: backfill release endpoint and listing order`.

---

## Self-review

- **Coverage:** API release of any source = Tasks 2, 3; newest first = Task 1; docs = Task 4.
- **Types:** `limit: int | None` is the same in `parse_release_params`, `ReleaseRequest` and the docs; `ordered: bool` only affects `add_pending`.
- **Risk to watch:** microsecond offsets and `found_at` collisions across two calls in the same microsecond are harmless (ties still break by `video_id`); `backfill.release` plus the default-on channel/docs releases share one 24 h allowance, so a manual release right after a channel call may release nothing.
