# Backfill: release by default Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A call to `POST /backfill/channels` or `POST /backfill/docs` processes the videos it found by default, and a switch turns that off.

**Architecture:** The job param `release` becomes `true | false | N`. The handler resolves it to a cap and releases only the pending videos that this call listed or scanned, so older pending videos are not touched. The API container cannot see worker settings, so it passes `true` through and the worker resolves the default cap from `BACKFILL_DAILY_LIMIT`.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2, Postgres 17 (marker `db`), pytest.

**Spec:** decisions of 2026-10-14 in this conversation (below); builds on `docs/superpowers/plans/2026-10-14-idea-catcher-backfill-api.md`.

## Decisions (from the user)

- `release` is on by default on **both** endpoints; an explicit switch turns it off.
- What is released: the **videos of this call** (listed by the channel, or found by the docs scan) that are `pending`. Not the oldest pending overall.
- The rolling 24 h cap **still applies** (`daily_allowance`). Videos over the cap stay `pending`.

## Global Constraints

- `release` values: omitted or `true` = release this call's pending videos up to `BACKFILL_DAILY_LIMIT` (worker setting, default 10); an int `N` (0..100 at the API) = the cap is `N`; `false` or `0` = release nothing. The 100 ceiling at the API stays.
- A dry run never releases. A job whose cap is used up releases nothing and still `Done`.
- A video listed in this call that is already `pending` from an earlier call (for example one made with `release: false`) IS released; a video that is already `released`, has a page, a job item or a clip is not.
- `catcher youtube import` (the CLI) is unchanged: it releases only with `--limit` or `--release`.
- No live calls in tests: no YouTube, LLM or network; fake extractor only.
- Docs plain English, no em dashes. Commit with `git add <paths>` then plain `git commit`; never stage `.github/` or plan files; never push; trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

- Call 1 with `release: false` lists 10 videos; call 2 (default) with the same channel releases those 10 (they are known but pending), up to the cap.
- Pending videos from other channels or from the docs scan are not released by a channel call.
- Cap used up by earlier releases: the call adds videos, releases none, and the result says so.
- `release: false` and `release: 0` and a dry run release nothing; a string, `101`, a negative number and `null`-vs-omitted behave as the contract says.
- The chain still queues a run only when `result["released"]` is non-empty.

---

### Task 1: Release only the videos of this call

**Files:**
- Modify: `src/catcher/modules/backfill/release.py` (`release`), `src/catcher/modules/backfill/service.py` (`release_within`), `src/catcher/modules/backfill/importer.py` (`ImportResult`, `run_import`)
- Test: `tests/integration/db/test_backfill_service.py`, `tests/integration/db/test_backfill_release.py` (use the existing file for `release` tests; find it with `grep -l "release(" tests -r`)

**Interfaces:**
- Produces:
  - `release(session, ideas_repo, limit, now, only: Collection[str] | None = None) -> ReleaseResult`: with `only`, the candidate rows are the `pending` rows whose `video_id` is in `only` (same order: `found_at`, then `video_id`); `None` keeps today's behavior.
  - `release_within(session, ideas, cap, now, only: Collection[str] | None = None) -> tuple[int, ReleaseResult]`.
  - `ImportResult.found_ids: list[str]` (every video id the scan found, sorted); `new_ids` stays.
  - `store.pending(session, limit=None, only=None) -> list[BackfillVideo]` gains the `only` filter.

- [ ] **Step 1: Write the failing tests:** `test_release_only_takes_the_given_ids` (5 pending, `only` has 2: exactly those 2 released, others pending), `test_release_only_respects_the_limit_and_order`, `test_release_only_ignores_ids_that_are_not_pending` (an id already `released` and an unknown id: nothing happens, no error), `test_release_without_only_is_unchanged`, `test_release_within_only_respects_the_rolling_cap`, `test_run_import_result_has_found_ids` (found 3, 1 known: `found_ids` has all 3, `new_ids` 2).
- [ ] **Step 2: Run to verify they fail:** `uv run pytest tests/integration/db -q -k "release or run_import"` fails on the missing parameter.
- [ ] **Step 3: Implement** the three signatures; `only` is applied in the `store.pending` query (`video_id IN (...)`), not in Python after a limit.
- [ ] **Step 4: Run the tests** (PASS) and `uv run pytest tests -q -k backfill`.
- [ ] **Step 5: Commit** `feat: release only the videos of one call`.

---

### Task 2: `release` as true, false or N in the job and the handlers

**Files:**
- Modify: `src/catcher/modules/worker/handlers_backfill.py` (`DocsParams`, `ChannelParams`, `parse_docs_params`, `parse_channel_params`, both handlers)
- Test: `tests/integration/db/test_handler_backfill_docs.py`, `tests/integration/db/test_handler_backfill_channel.py`

**Interfaces:**
- Consumes: Task 1's `release_within(..., only=)`, `ImportResult.found_ids`.
- Produces:
  - `DocsParams.release` and `ChannelParams.release` become `bool | int` with default `True` (type `bool | int`, never `None`); the parsers accept `None` as the default `True`, a real `bool`, or an int >= 0 (not a bool-as-int confusion: `True`/`False` are kept as bools); anything else is `ValueError` naming `release`.
  - `release_cap(value: bool | int, default_limit: int) -> int`: `True` -> `default_limit`, `False` -> `0`, int -> that int. Lives in `handlers_backfill.py` (private use by both handlers).
  - Result keys unchanged. `allowance` is the allowance computed (0 when released nothing because of a `False`/`0` value; `None` only for a dry run).

- [ ] **Step 1: Write the failing tests:**
  - Parsers: `release` omitted, `None`, `True`, `False`, `0`, `5` accepted; `"5"`, `-1`, `1.5` rejected (both job types).
  - Docs handler: `test_default_releases_this_scans_pending_up_to_the_default_limit` (settings `backfill_daily_limit=2`, scan finds 3 new: 2 released, `still_pending == 1`); `test_release_false_adds_but_releases_nothing`; `test_release_int_is_the_cap`; `test_a_pending_video_from_an_earlier_call_that_the_scan_finds_again_is_released`; `test_pending_videos_the_scan_did_not_find_are_not_released`; `test_dry_run_releases_nothing_by_default`.
  - Channel handler: the same five behaviors with a fake extractor, plus `test_cap_used_up_adds_but_releases_nothing` (3 released an hour ago, `release=3`: videos stored, `released == []`).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** `release_cap`, the parser change, and in both handlers call `release_within(session, ctx.ideas, cap, now, only=<found ids>)` where `<found ids>` is `ImportResult.found_ids` for docs and the listed `ids` for channel. A cap of `0` skips the release (no `release_within` call), as `None` does today.
- [ ] **Step 4: Run the tests** (PASS), then `uv run pytest tests -q -k "backfill or chain"`.
- [ ] **Step 5: Commit** `feat: backfill jobs release by default`.

---

### Task 3: The API takes `release` as true, false or N

**Files:**
- Modify: `src/catcher/api/routes_backfill.py` (`DocsRequest`, `ChannelsRequest`, the params built for the jobs)
- Test: `tests/integration/db/test_api_backfill.py`

**Interfaces:**
- Consumes: Task 2's job params.
- Produces: `release: StrictBool | Annotated[StrictInt, Field(ge=0, le=MAX_RELEASE)] | None = None` on both request models. Omitted or `null` adds NO `release` key to the job params (the handler default `True` applies); `true`, `false` and `N` are stored as given.

- [ ] **Step 1: Write the failing tests:** body without `release` stores params with no `release` key; `{"release": false}` stores `false`; `{"release": true}` stores `true`; `{"release": 5}` stores `5`; `{"release": 100}` accepted and `101`, `-1`, `"yes"`, `1.5` are 422 on both endpoints; the existing tests that assumed `release` null means "release nothing" are updated to the new meaning.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement the model field and the param building** (keep `MAX_RELEASE = 100`).
- [ ] **Step 4: Run** `uv run pytest tests/integration/db -q -k api` (PASS).
- [ ] **Step 5: Commit** `feat: backfill API releases by default`.

---

### Task 4: Docs, requests and the final check

**Files:**
- Modify: `docs/idea-catcher-how-to-run-api.md`, `docs/idea-catcher-how-to-run-stage-b.md`, `docs/idea-catcher-service-architecture.md`, `tests/manual/catcher-api.http`

- [ ] **Step 1: Update the docs and requests.** Say plainly: both endpoints release by default; `release: false` (or `0`) only adds to the backlog; `release: N` sets the cap to N; the default cap is `BACKFILL_DAILY_LIMIT` of the worker (the API container cannot see it); the call releases only the pending videos it listed or scanned, so a call made with `release: false` can be followed by a default call that releases them; the rolling 24 h cap still applies and leftovers stay `pending`; the CLI `catcher youtube import` is unchanged; `catcher jobs add backfill.docs|backfill.channel` now releases by default unless `--param release=false`. Replace every sentence that says "no release by default" or "release null releases nothing". Add two `.http` requests (default, `release: false`) for the channel endpoint, keeping the "calls YouTube, hand test only" note.
- [ ] **Step 2: Run the full gate:** `uv run ruff check . && uv run pyright && uv run pytest -q && uv run pytest tests/integration/db -q` all pass (`ruff format --check` flags two untouched files: `docs/idea-catcher-pipeline.md` and `docs/superpowers/plans/2026-10-06-idea-catcher-stage-b5b-parity-table.md`; leave them).
- [ ] **Step 3: Commit** `docs: backfill releases by default`.

---

## Self-review

- **Coverage:** default on for both endpoints = Tasks 2, 3; switch off = Tasks 2, 3; this call's videos = Tasks 1, 2; cap applies = Task 2 tests; docs = Task 4.
- **Types:** `release: bool | int` is the same in the parsers (Task 2), the API models (Task 3) and the docs; `only: Collection[str] | None` is the same in `store.pending`, `release`, `release_within`.
- **Risk to watch:** a first docs scan on a big docs repo now releases up to `BACKFILL_DAILY_LIMIT` (default 10) videos without being asked; the user chose this, and the cap bounds it.
