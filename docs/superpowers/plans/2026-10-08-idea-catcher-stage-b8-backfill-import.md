# Idea Catcher Stage B8: the Backfill Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `catcher youtube import` finds the YouTube links in `epiaku-docs` that have no page yet, keeps them in a database backlog, and releases a limited number per run into the normal pipeline as low-priority work behind new clips.

**Architecture:** A scan of the docs repo (no YouTube call) fills a new table `backfill_videos` (`pending`). A release step (`--limit N`) writes one small clip note per video into `idea-bucket/inbox/clippings/` (the same file a Web Clipper capture would be) with a `backfill: true` marker and marks the video `released`. The normal `pipeline.run` stages those notes like any capture; because of the marker the item gets `origin = 'backfill'` and every job of that item (`youtube.fetch`, then `llm.reason`) is queued with a low priority, so new clips always go first. An optional paced channel listing adds a channel's video ids to the backlog.

**Tech Stack:** Python 3.12, SQLAlchemy 2 (sync) + psycopg 3, Alembic (migration `0006`), Typer, yt-dlp (channel listing only), pytest + testcontainers.

**Spec:** `docs/idea-catcher-service-architecture.md` row B8 of the Stage B table (about line 717) and `docs/idea-catcher-youtube-bans-and-queue-options.md` "The first big batch (backfill)" (about lines 255-269: import command, skip ids that already have a page, low-priority jobs, budget or `--limit` per day, the channel listing is riskier: list once, flat, paced, store the ids in the database, then add as low-priority jobs; try on a small channel by hand first).

## Global Constraints

- Postgres is the single truth: the backlog is a table, not a file.
- New clips keep their normal priority (0) and go first; backfill work runs at priority **-10** (`BACKFILL_PRIORITY`). The claim order is `priority DESC, run_after, created_at` (unchanged).
- The YouTube gap and breaker apply to backfill fetches exactly as to any fetch (the gate is unchanged). The channel listing goes through the same gate.
- The scan, the table, the release and the priority make **no YouTube call and no LLM call**; only `--channel` calls YouTube (yt-dlp) and agents never run it: tests use a fake extractor, the user does the hand test.
- No live LLM or YouTube call in any test; the network guard stays on; db tests use testcontainers, never the user's `catcher-db`; never read `.env`; never touch `tests/data`, `tmp/ic`, `~/.catcher`.
- Agents commit locally, never push (the user pushes).

## Decisions made in this plan (defaults; the user can change them)

1. **A table `backfill_videos`** (migration `0006`): `video_id text primary key`, `source text not null` (`docs` or `channel`), `found_in text` (first doc path or channel URL), `status text not null` (`pending`, `released`), `found_at timestamptz`, `released_at timestamptz null`. A video id is in it once.
2. **What counts as "already has a page":** the id appears as `video_id:` in the frontmatter of any markdown file under the docs repo (our pages: `video_id`, also the base id of `id: <vid>-gemini`), OR as `doc_id`/`<vid>-gemini` of a `job_items` row (in flight or done), OR as the `source` video of a clip anywhere in the idea-bucket (`inbox/`, `archive/`, `output/`, `failed/`, `duplicates/`), OR already in `backfill_videos`.
3. **Which links are found:** every `youtube.com/watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`, `/live/` link in the body or frontmatter of any markdown file under the docs repo (use `catcher.modules.youtube.urls.video_id` and the URL regex; a playlist or channel link without a video id is not a video). A page that only links to another video's page still counts the target as a candidate unless that target has a page.
4. **Release writes a clip note** `inbox/clippings/youtube source - <video id>.md` with frontmatter `source: https://www.youtube.com/watch?v=<id>`, `tags: [clippings]`, `backfill: true`, `created: <today>`; the existing doctype detection turns it into a YouTube page. Written atomically (temp file + rename), never overwriting an existing file (an existing note of that name marks the video `released` without a write).
5. **`--limit N` is a cap per rolling 24 hours** (user concern 2026-10-08: avoid a YouTube ban on the first big backfill): the release counts the rows with `released_at` in the last 24 h and releases at most `N - already released` more, so running the command twice in a day cannot exceed `N`. `N` defaults to `BACKFILL_DAILY_LIMIT` (setting, default 10) when `--limit` is not given and a release is asked for with `--release`; a release needs `--limit` or `--release`. Docs recommend a staged start: 5 on day one, then 20, then 50, and raising `YOUTUBE_MIN_GAP_S` before raising the limit. The YouTube gate paces the fetches whatever the backlog size. Without either flag the command only scans and stores; `catcher youtube import` without `--limit` only scans and stores (and says how many are pending). There is no daily automation in B8 (the user runs it once a day for a while: open item: a schedule).
6. **Priority propagation:** the marker `backfill: true` on a note makes `scan` set the item's `origin = 'backfill'` (the column and its CHECK exist since `0003`); every job queued for an item with that origin gets `priority = -10`. This holds whoever stages the note (a scheduled run or `catcher run pipeline`).
7. **Channel listing** (`--channel URL`, repeatable, `--max-videos N` default 200): one yt-dlp flat-playlist call (`extract_flat`, `playlistend=N`, `sleep_interval_requests` from `YOUTUBE_REQUEST_DELAY_S`), taking **one gate slot** first (a closed gate or breaker stops it with a clear message and exit 1), the ids go into the backlog like docs ids. Any 429-like error opens the breaker the way a fetch does. No automatic retry.
8. **No LLM money budget in B8 beyond `--limit`** and the existing per-backend budget block (B5): open item.

## Review Focus

- A docs repo with thousands of markdown files, odd encodings, files with broken frontmatter, symlinks and a `.git` folder: the scan must skip unreadable files with a count, never crash, never follow a link outside the repo, never read `.git`.
- The same video linked from 40 pages, in `watch?v=ID&t=30s`, `youtu.be/ID?si=…`, `/shorts/ID` and `m.youtube.com` forms: one backlog row.
- Release twice in a row (or two commands racing): no video is released twice, no note is overwritten, a crash between writing the note and marking the row leaves a state the next release repairs (the note exists, row still `pending`: the next run marks it `released` without writing).
- A new clip captured while 200 backfill notes wait: its fetch and reason jobs run first; the backfill never starves it (priority) and never bypasses the gate.
- A video that is private/removed/no captions: the normal item path handles it (deferred or failed with a reason); the backlog row stays `released` (the item owns the outcome).
- `--channel` when the gate is closed or the breaker is open: nothing is called, nothing stored, exit 1 with the reason.

## File structure

- Create `migrations/versions/0006_backfill_videos.py`, `src/catcher/modules/backfill/__init__.py`, `store.py` (table access), `scan.py` (docs scan, known ids), `release.py` (note writing), `channel.py` (the listing).
- Modify `src/catcher/modules/queue/models.py` (`BackfillVideo`), `src/catcher/core/config.py` (`backfill_priority: int = -10`), `src/catcher/modules/worker/handlers_pipeline.py` and the scan/stage code (marker -> `origin`; priority on follow-up jobs), `src/catcher/cli.py` (`youtube import`, `youtube backlog`), docs.
- Tests: `tests/integration/db/test_backfill_store.py`, `tests/unit/test_backfill_scan.py`, `tests/integration/db/test_backfill_import_cli.py`, `tests/integration/db/test_backfill_priority.py`, `tests/unit/test_backfill_channel.py`.

---

### Task 1: The table and the store (strict review: migration)

**Files:**
- Create: `migrations/versions/0006_backfill_videos.py`, `src/catcher/modules/backfill/__init__.py`, `src/catcher/modules/backfill/store.py`
- Modify: `src/catcher/modules/queue/models.py`
- Test: `tests/integration/db/test_backfill_store.py`, extend `tests/integration/db/test_schema.py`

**Interfaces:**
- Produces model `BackfillVideo(video_id: str pk, source: str, found_in: str | None, status: str, found_at: datetime, released_at: datetime | None)` with CHECKs on `source in ('docs','channel')` and `status in ('pending','released')`.
- Produces in `store.py`: `add_pending(session, found: Mapping[str, tuple[str, str]], now: datetime) -> int` (maps video id to `(source, found_in)`, inserts the missing ones with `ON CONFLICT DO NOTHING`, returns how many were new); `pending(session, limit: int | None = None) -> list[BackfillVideo]` (oldest `found_at` first, then id); `mark_released(session, video_id: str, now: datetime) -> bool` (only a `pending` row; returns whether it changed; idempotent); `counts(session) -> dict[str, int]` (`{"pending": n, "released": m}`); `known(session) -> set[str]`.

- [ ] **Step 1: Write the failing tests:** `test_the_migration_upgrades_from_head_and_downgrades` (add to `test_schema.py` in its style), `test_add_pending_inserts_new_ids_only_and_counts_them`, `test_add_pending_twice_is_a_noop`, `test_pending_is_oldest_first_and_honours_the_limit`, `test_mark_released_changes_a_pending_row_once`, `test_mark_released_on_an_unknown_id_is_false`, `test_the_checks_refuse_a_bad_status_or_source`, `test_two_sessions_adding_the_same_id_do_not_fail`.
- [ ] **Step 2: Run** `uv run pytest tests/integration/db/test_backfill_store.py tests/integration/db/test_schema.py -x -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the migration (revision after `0005`; follow the style of `0005`), the model and the store.
- [ ] **Step 4: Run** the tests. Expected: PASS. Then `scripts/check` once.
- [ ] **Step 5: Commit** `feat: the backfill backlog table and its store`.

### Task 2: The docs scan

**Files:**
- Create: `src/catcher/modules/backfill/scan.py`
- Test: `tests/unit/test_backfill_scan.py` (temp trees only), `tests/integration/db/test_backfill_known_ids.py`

**Interfaces:**
- Produces `@dataclass(frozen=True) class ScanResult: found: dict[str, str]` (video id -> first doc path relative to the repo), `files: int`, `unreadable: int`.
- Produces `scan_docs(docs_repo: Path) -> ScanResult`: walks every `*.md` under the repo (skipping `.git`, not following symlinks that leave the repo), reads UTF-8 with `errors="replace"`, collects video ids from every YouTube URL in frontmatter and body (decision 3).
- Produces `page_ids(docs_repo: Path) -> set[str]`: video ids that already have a page by decision 2's first rule (`video_id:` frontmatter; base id of `id: <vid>-gemini`); a file with broken frontmatter is skipped (counted as unreadable by the caller via the same walk: share the walk).
- Produces `known_ids(session: Session, ideas_repo: Path, docs_repo: Path) -> set[str]`: the union of `page_ids`, the video ids in `job_items.doc_id` (strip a `-gemini` suffix), the source video ids of clip files under the idea-bucket `inbox/`, `archive/`, `output/`, `failed/`, `duplicates/`, and `store.known(session)`.

- [ ] **Step 1: Write the failing tests:** `test_the_url_forms_all_give_one_id` (watch with `&t=30s`, `youtu.be/ID?si=x`, `/shorts/ID`, `m.youtube.com`, `/embed/ID`, `/live/ID`), `test_a_playlist_or_channel_link_is_not_a_video`, `test_the_same_video_in_many_pages_is_one_row_with_the_first_page`, `test_frontmatter_source_and_body_links_are_both_found`, `test_git_and_symlinks_out_of_the_repo_are_not_read`, `test_a_file_with_broken_frontmatter_or_bad_bytes_is_skipped_and_counted`, `test_page_ids_reads_video_id_and_the_gemini_base_id`, `test_known_ids_unions_pages_job_items_ideas_clips_and_the_backlog`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** Reuse `catcher.modules.youtube.urls` and the frontmatter reader in `catcher.core.frontmatter`; one walk shared by `scan_docs` and `page_ids`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: scan the docs for YouTube links that have no page`.

### Task 3: `catcher youtube import` and `catcher youtube backlog` (scan and store)

**Files:**
- Modify: `src/catcher/cli.py` (`youtube_app`)
- Test: `tests/integration/db/test_backfill_import_cli.py`

**Interfaces:**
- Consumes Tasks 1 and 2.
- Produces `catcher youtube import [--docs PATH] [--ideas PATH] [--dry-run]`: needs the database (exit 2 as the other commands do); scans, subtracts `known_ids`, stores the rest with `source = 'docs'` and `found_in` = the first doc path; prints `scanned N file(s), found M video(s), K already have a page, J new in the backlog (P pending in all)`; `--dry-run` prints the same counts and writes nothing; it takes no lock and starts no worker. Exit codes: 0 ok, 2 bad paths/database.
- Produces `catcher youtube backlog [--limit N]`: counts per status and the oldest N pending (video id, found in); read-only.

- [ ] **Step 1: Write the failing tests:** `test_import_stores_only_videos_without_a_page`, `test_import_twice_adds_nothing_the_second_time`, `test_dry_run_writes_nothing_and_prints_the_counts`, `test_import_skips_videos_already_in_job_items_or_the_idea_bucket`, `test_import_exit_2_for_a_missing_docs_folder_or_an_unreachable_database`, `test_backlog_lists_counts_and_the_oldest_pending`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the two commands in the existing `youtube_app` (see `youtube facts` and `youtube gate` for the style and database helpers).
- [ ] **Step 4: Run** the tests. Expected: PASS. Then `scripts/check` once.
- [ ] **Step 5: Commit** `feat: catcher youtube import fills the backfill backlog from the docs`.

### Task 4: Release, the marker and the low priority (strict review: queue, data safety)

**Files:**
- Create: `src/catcher/modules/backfill/release.py`
- Modify: `src/catcher/cli.py` (`import --limit`, `--release`), `src/catcher/core/config.py` (`backfill_priority`, `backfill_daily_limit`), the inbox scan / staging code and the handlers that queue `youtube.fetch` and `llm.reason` (find them: `handle_pipeline_run`, `handle_youtube_fetch` in `handlers_pipeline.py`; the item creation in `queue/items.py` takes `origin`)
- Test: `tests/integration/db/test_backfill_release.py`, `tests/integration/db/test_backfill_priority.py`

**Interfaces:**
- Produces `release(session: Session, ideas_repo: Path, limit: int, now: datetime) -> ReleaseResult` with `ReleaseResult(released: list[str], repaired: list[str])`: takes up to `limit` oldest `pending` rows; for each writes the note of decision 4 atomically unless the file exists (then it is a repair), then `mark_released`; a failure to write one note is logged and that row stays `pending`. The ideas repo is not committed here (publish commits it).
- Produces `Settings.backfill_priority: int = -10` (`BACKFILL_PRIORITY`) and `Settings.backfill_daily_limit: int = Field(default=10, ge=0)` (`BACKFILL_DAILY_LIMIT`); `store.released_since(session, since: datetime) -> int` (count of rows with `released_at >= since`); `release(..., limit)` receives the already-reduced allowance (`limit - released_since(now - 24h)`, never below 0) from the CLI.
- Produces: a note with frontmatter `backfill: true` is staged with `origin='backfill'`; every job queued for an item whose origin is `backfill` has `priority = settings.backfill_priority`; items without the marker are unchanged (priority 0).
- `catcher youtube import --limit N` runs the scan and store first (as Task 3), then `release(..., limit=N)`; prints `released R video(s) into inbox/clippings/ (P still pending)`; with `--dry-run` it prints what it would release and writes nothing.

- [ ] **Step 1: Write the failing tests:** `test_release_writes_a_clip_note_and_marks_the_row_released` (frontmatter exact), `test_release_honours_the_limit_and_the_oldest_first_order`, `test_the_limit_is_per_rolling_24_hours_so_a_second_run_releases_only_the_rest`, `test_after_24_hours_the_budget_comes_back`, `test_release_without_limit_uses_BACKFILL_DAILY_LIMIT`, `test_release_twice_does_not_release_a_video_twice`, `test_an_existing_note_is_never_overwritten_and_the_row_is_repaired`, `test_a_write_failure_leaves_the_row_pending_and_the_others_released`, `test_the_released_note_is_detected_as_a_youtube_clip_and_staged_with_origin_backfill`, `test_follow_up_jobs_of_a_backfill_item_have_the_low_priority_and_a_normal_clip_has_zero`, `test_a_new_clip_is_claimed_before_waiting_backfill_jobs` (real claim order with both kinds queued), `test_the_gate_still_defers_a_backfill_fetch` (closed gate: the job waits; no attempt counted), `test_import_limit_prints_the_release_line_and_dry_run_writes_nothing`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** Find where the scan builds the `Note` and where the item row is created (`queue/items.py` `origin=`); read the marker from the frontmatter there; pass the priority where the handlers `queue.enqueue(...)` the next job for that item (read the item's origin from the row). Do not change the priority of anything else.
- [ ] **Step 4: Run** the tests, then `uv run pytest tests/integration/db -q -x` and `scripts/check` once.
- [ ] **Step 5: Commit** `feat: catcher youtube import --limit releases backfill videos as low-priority clips`.

### Task 5: The paced channel listing (strict review: YouTube)

**Files:**
- Create: `src/catcher/modules/backfill/channel.py`
- Modify: `src/catcher/cli.py` (`import --channel`, `--max-videos`)
- Test: `tests/unit/test_backfill_channel.py`, extend `tests/integration/db/test_backfill_import_cli.py`

**Interfaces:**
- Consumes the gate (`PostgresGate` / `YoutubeAccess` as `youtube facts` builds it: read `catcher youtube facts` in `cli.py` and `modules/youtube/access.py`) and `store.add_pending`.
- Produces `list_channel(url: str, *, max_videos: int, extractor: Callable[..., dict[str, Any]]) -> list[str]`: the extractor is injected (default wraps `yt_dlp.YoutubeDL({"extract_flat": True, "playlistend": max_videos, "skip_download": True, "quiet": True, "sleep_interval_requests": <YOUTUBE_REQUEST_DELAY_S>})`); returns video ids of the entries (order kept, no duplicates, ids validated by `video_id`'s 11-character rule); raises `ChannelListingError(message)` on an extractor error (a 429-like error carries a flag the caller uses to open the breaker).
- Produces `catcher youtube import --channel URL [--channel URL ...] [--max-videos N=200]`: for each URL takes one gate slot first (closed gate or open breaker: print the reason, store nothing, exit 1), lists, stores ids as `source='channel'` with the channel URL in `found_in` (minus known ids), and on a 429-like error opens the breaker exactly as a failed fetch does and exits 1. `--dry-run` lists nothing (it says it would list N channel(s) and takes no gate slot).

- [ ] **Step 1: Write the failing tests** (fake extractor only; no yt-dlp call): `test_list_channel_returns_valid_ids_in_order_without_duplicates`, `test_list_channel_passes_max_videos_and_the_request_delay_to_the_extractor`, `test_a_bad_entry_or_a_non_video_url_in_the_listing_is_skipped`, `test_an_extractor_error_raises_channel_listing_error_and_a_429_sets_the_flag`, `test_import_channel_takes_one_gate_slot_and_stores_the_ids`, `test_import_channel_with_a_closed_gate_calls_nothing_and_stores_nothing`, `test_a_429_opens_the_breaker_and_exits_1`, `test_dry_run_with_a_channel_takes_no_gate_slot`, `test_the_network_guard_stays_on` (importing the module and running the default extractor wiring without calling it does no network).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** The CLI builds the real extractor; tests inject the fake. Never call yt-dlp in a test.
- [ ] **Step 4: Run** the tests; then `scripts/check` once.
- [ ] **Step 5: Commit** `feat: a paced channel listing adds a channel's videos to the backfill backlog`.

### Task 6: Docs and an offline real check (strict review)

**Files:**
- Modify: `docs/idea-catcher-how-to-run-stage-b.md` (a "Backfill" section: the scan, `backlog`, `--limit`, the priority, the channel listing and how to hand-test it on a small channel, budget advice using the table in the YouTube doc), `docs/idea-catcher-how-to-run.md` (pointer), `docs/idea-catcher-service-architecture.md` (B8 built with the date, decisions, "Open items after B8"), `docs/idea-catcher-youtube-bans-and-queue-options.md` (remove "Not built yet: the backfill import" and the "Still open" lines this plan settles), `README.md`, `.env.example` (`BACKFILL_PRIORITY`).

- [ ] **Step 1: Verify for real, offline:** a throwaway Postgres on a random port (never `catcher-db`; remove it afterwards), a throwaway copy of the test repos in the scratchpad plus a docs copy with a few YouTube links added by the check (some with pages, some without, forms `watch?v=`, `youtu.be`, `shorts`), env `OPENAI_API_KEY=""`, `FREELLMAPI_URL=http://127.0.0.1:1/v1`, `YOUTUBE_OFFLINE=1`; run `catcher db upgrade`, `catcher youtube import --dry-run`, `catcher youtube import --limit 3`, `catcher youtube backlog`, then `catcher run pipeline` (the LLM and YouTube are unreachable so the clips defer: look at the `jobs list` priorities: backfill jobs at -10, a fresh normal clip at 0, and that a normal clip is claimed first), and a second `import --limit 3` (no double release). `--channel` is NOT run by agents.
- [ ] **Step 2: Write the docs** from what was verified and what the tests pin.
- [ ] **Step 3: Run** `scripts/check` once.
- [ ] **Step 4: Commit** `docs: Stage B8 built`.

## Self-review

- **Spec coverage:** import finds links in the docs (Task 2, 3), skips ids that already have a page (Task 2: `known_ids`), adds the rest as low-priority work (Task 4), a paced channel listing (Task 5), a `--limit` (Task 4), a budget beyond `--limit`: named as an open item (decision 8). The research doc's "store the ids in the database, then add as low-priority jobs" is the table plus release.
- **Types and names:** `BackfillVideo`, `add_pending`, `pending`, `mark_released`, `counts`, `known`, `scan_docs`, `page_ids`, `known_ids`, `release`, `list_channel`, `BACKFILL_PRIORITY` are used with the same names across tasks.
- **Review Focus:** big or odd docs trees (Task 2), URL forms (Task 2), release races and crash repair (Task 4), new clips first and the gate (Task 4), private/removed videos (the item path; Task 6 observes deferral), closed gate for `--channel` (Task 5).
- **Open questions for the user:** the eight decisions at the top. The ones most likely to change: releasing by writing clip notes into `inbox/clippings/` (versus queueing jobs directly), `-10` as the backfill priority, and no daily automation in B8.
