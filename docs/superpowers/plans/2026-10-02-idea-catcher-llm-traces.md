# Idea Catcher: keep the raw LLM reply (LLM traces) and replay it

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every LLM call leaves a trace file next to the other repo state (like `facts/<video id>.json` does for YouTube): the raw replies, which model and prompt version answered, and the validated output. A run then **reads a saved good reply before calling the model** (like it reads saved facts before calling YouTube), so reruns are free, a frozen real run is the mock data for tests, and the record is there to debug a result.

**Architecture:** `reason()` (the one LLM function) gets two optional hooks: a `recorder` that receives an `LlmTrace` after a real call, and a `replayer` that supplies saved raw replies instead of the backend (the replies still go through the same JSON parsing, validation and retry code). A `TraceStore` writes and finds `llm/<subfolder>/<calculated name>.json` in the idea-bucket repo. `ask_llm` and `run_pipeline` wire it in; the files are committed with the run, like the saved YouTube facts.

**Tech Stack:** Python 3.12, uv, Pydantic v2, pytest. No database, no network.

**Spec:** this plan is the spec for a new feature; related design: `docs/idea-catcher-service-architecture.md` (LLM Step & Profiles; the saved facts) and `docs/idea-catcher-youtube-bans-and-queue-options.md` (saved facts as the model).

## Decisions taken (defaults; veto any before execution)

1. **Always on:** `LLM_TRACE=true` by default. Set `LLM_TRACE=false` to switch it off.
2. **Where:** `llm/<subfolder>/<calculated name>.json` at the root of the idea-bucket repo, the same subfolder and calculated name as `archive/`, `output/` and `failed/` (`note.target_rel` with `.json`). A requeue overwrites the same file.
3. **What is in it:** `version` (1), `saved_at`, `task`, `profile`, `backend`, `model`, `prompt_version`, `content_key`, `prompt_sha256`, `attempts` (a list: `reply`, `tokens_in`, `tokens_out`, `duration_ms`, `model`, `error`), `outcome` (`ok`, `invalid_output`, `backend_error`), `error`, `output` (the validated JSON, when ok). **The prompt text itself is not saved** unless `LLM_TRACE_PROMPT=true` (a prompt holds the whole transcript and can be 100 KB; the hash plus the prompt version identify it).
4. **Saved-reply key (decided by the user, 2026-10-02):** a saved reply is reused only when the **task, the profile, the prompt version and the `content_key`** all match (`content_key = sha256(task + NUL + body + NUL + transcript_or_empty)`: the document text, not the document id, which is random for a note). A different profile or a prompt version bump is a miss. Only traces that ended `ok` are reused. A glossary, business-context or tag-list change does not change the key (tags are normalised again when the page is rendered); to refresh, delete the file or use `--refresh-llm`.
4b. **`--requeue` is unchanged:** it reuses a good saved reply like any other run. Fresh output only with an explicit `--refresh-llm` (combinable with `--requeue`) or by deleting the file. Nothing is implicit. `LLM_CACHE=false` switches the reading off.
5. **Committed with the run:** the trace files are added to the idea-bucket commit like `facts/`. A dry run writes nothing.
6. **Cleanup comes later:** a `catcher cleanup` for old `facts/`, `llm/`, `archive/`, `output/` records (dry run first, by age and status) is a recorded backlog item, not part of this plan. Note: deleting a saved YouTube facts file makes a later requeue call YouTube again.

## Global Constraints

- Stage A behaviour is frozen: `uv run pytest tests/unit tests/component tests/integration -q` stays green with the existing tests unchanged (new tests are added). Today's pages, folders and commits are the same except for the new `llm/` files.
- A trace failure must never fail a document: if writing the trace raises, log a warning and carry on.
- A trace never contains an API key or any secret (it holds replies and metadata only).
- No live LLM or YouTube call in any test (fake backends; the replay tests use no backend at all).
- Line length 110; ruff (`E F I UP B SIM`); pyright standard on `src`.
- Commit only on the user's go; no push; end commit messages with the attribution line the session requires.

## Review Focus

1. **A failed call leaves a trace:** invalid output twice, or a backend error, still writes a trace with the replies and the error (that is when it matters most for debugging) (Tasks 1, 3).
2. **Reuse is exact:** a run that reuses saved replies gives byte-identical pages, and a backend that raises proves no model was called (Task 4).
3. **A broken or missing trace file never breaks a run:** unreadable JSON, a read-only folder, a changed schema version (Tasks 2, 3).
4. **A requeue and a dry run:** a requeue overwrites the same file, a dry run writes nothing, `LLM_TRACE=false` writes nothing (Task 3).
5. **No secrets and no prompt text by default** (Tasks 1, 3).

---

### Task 1: `reason()` records a trace and can replay one

**Files:**
- Modify: `src/catcher/modules/llm/service.py`
- Test: `tests/component/test_llm_trace_hooks.py`

**Interfaces:**
- Produces: `@dataclass class LlmAttempt: reply: str | None; tokens_in: int | None; tokens_out: int | None; duration_ms: int | None; model: str | None; error: str | None`; `@dataclass class LlmTrace: task: str; profile: str; backend: str; model: str | None; prompt_version: str; content_key: str; prompt_sha256: str; prompt: str | None; attempts: list[LlmAttempt]; outcome: Literal["ok", "invalid_output", "backend_error"]; error: str | None; output: dict[str, Any] | None`; `def content_key(task: str, body: str, transcript: str | None) -> str`; `Recorder = Callable[[LlmTrace], None]`; `Replayer = Callable[[LlmRequest, str], list[str] | None]` (arguments: the request and its `content_key`; returns the raw replies in order, or None for "no recording: call the backend"); `reason(req, *, profiles, backends, recorder: Recorder | None = None, replayer: Replayer | None = None, keep_prompt: bool = False)`. `content_key` is computed from `req.input["body"]` and `req.input.get("transcript")`. With replies from the replayer the backend is **not built or called**; each reply goes through `extract_json` + `model_validate_json` and the existing second-attempt rule (a second reply is used for attempt 2; running out of replies raises `InvalidOutput("replay has no more replies")`). The recorder is called once, in a `finally`, whenever at least one attempt was started, with the outcome (a backend exception becomes `backend_error` with `error=str(e)` and then propagates unchanged). A recorder that raises is logged and ignored.

- [ ] **Step 1: Write the failing test.** With the fake backend: `test_a_successful_call_records_one_attempt_and_the_validated_output`; `test_an_invalid_reply_then_a_valid_one_records_two_attempts`; `test_two_invalid_replies_record_invalid_output_and_still_raise`; `test_a_backend_error_records_backend_error_and_propagates`; `test_the_prompt_is_only_kept_when_asked` (`prompt is None` by default, equal to the rendered prompt with `keep_prompt=True`, and `prompt_sha256` set either way); `test_content_key_ignores_everything_but_task_body_and_transcript`; `test_a_replayer_supplies_the_replies_and_the_backend_is_never_built` (a `backends` factory that raises if called); `test_a_replayed_invalid_reply_goes_through_the_same_retry_rule`; `test_replay_without_enough_replies_raises_invalid_output`; `test_a_recorder_that_raises_does_not_break_the_call`.
- [ ] **Step 2: Run** `uv run pytest tests/component/test_llm_trace_hooks.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the dataclasses, `content_key` and the hooks in `reason()`; keep `reason()`'s behaviour without hooks identical (the existing tests are the net).
- [ ] **Step 4: Run** `uv run pytest tests/unit tests/component -q` and `uv run pyright`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: reason() can record a trace and replay one`.

### Task 2: the trace store

**Files:**
- Create: `src/catcher/modules/llm/trace.py`
- Test: `tests/component/test_llm_trace_store.py`

**Interfaces:**
- Produces: `LLM_DIR = "llm"`; `class TraceFile(BaseModel)` (the JSON shape of decision 3, `version: int = 1`, `saved_at: str`); `class TraceStore: def __init__(self, directory: Path) -> None`; `def path_for(self, target_rel: Path) -> Path` (`directory / target_rel` with the suffix `.json`); `def put(self, target_rel: Path, trace: LlmTrace, *, now: datetime) -> Path` (atomic write with `catcher.core.files.write_atomic`, indented JSON, `ensure_ascii=False`); `def find(self, task: str, content_key: str) -> list[str] | None` (the replies of the **newest** trace file under the directory whose `task` and `content_key` match and whose outcome is `ok` or `invalid_output`, or None; unreadable or wrong-version files are skipped with a warning); `def replayer(self) -> Replayer`.

- [ ] **Step 1: Write the failing test.** `test_put_writes_the_file_at_the_calculated_name_and_it_round_trips`; `test_put_overwrites_the_same_file_on_a_requeue`; `test_find_returns_the_replies_for_a_matching_task_and_key`; `test_find_returns_none_for_another_key_or_task`; `test_find_prefers_the_newest_file`; `test_an_unreadable_or_future_version_file_is_skipped_not_fatal` (Review Focus #3); `test_a_trace_never_holds_the_api_key` (put a trace made through a backend whose settings carry `sk-secret`: assert the string is absent from the file); `test_put_leaves_no_temp_file`.
- [ ] **Step 2: Run** `uv run pytest tests/component/test_llm_trace_store.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the store; `find` scans `directory.rglob("*.json")` (small folders; no index file).
- [ ] **Step 4: Run** the same command and `uv run pyright`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: a store for LLM traces`.

### Task 3: wire it into the pipeline

**Files:**
- Modify: `src/catcher/core/config.py` (`llm_trace: bool = True`, `llm_trace_prompt: bool = False`), `src/catcher/modules/pipeline/process.py` (`ProcessOptions.llm_dir: Path | None = None`; `ask_llm` builds the recorder from `TraceStore(llm_dir)` and the note's `target_rel` when `llm_dir` is set, `svc.settings.llm_trace` is true and the run is not a dry run; passes `keep_prompt=svc.settings.llm_trace_prompt`), `src/catcher/modules/pipeline/run.py` (`ProcessOptions(llm_dir=ideas / LLM_DIR)`; after each note, in the existing `finally`, add the trace file to `touched_ideas` when it exists, like the facts file), `.env.example`
- Test: `tests/integration/git/test_llm_traces_in_run.py` (new file; the Stage A tests stay untouched), `tests/unit/test_config.py` (new test functions only)

**Interfaces:**
- Consumes: Task 1's `reason(recorder=...)`, Task 2's `TraceStore`.
- Produces: after `run_pipeline`, a trace `llm/<subfolder>/<calculated name>.json` for every document that reached the LLM, committed in the idea-bucket commit. The recorder builds a `TraceFile` with `saved_at` from the run's clock (`datetime.now(UTC)`) and swallows and logs any writing error (Review Focus #3).

- [ ] **Step 1: Write the failing test.** On the git `repos` fixture pattern of `tests/integration/git/test_run.py` (copy the small seed helper into the new test file, do not import from it): `test_a_run_writes_a_trace_per_document_and_commits_it`; `test_the_trace_name_matches_the_archive_and_output_name`; `test_a_requeue_overwrites_the_same_trace_file`; `test_a_failed_llm_call_still_leaves_a_trace_with_the_error` (backend returning garbage twice: `outcome == "invalid_output"`, both replies present; backend raising `BackendUnavailable`: `backend_error`) (Review Focus #1); `test_a_dry_run_writes_no_trace`; `test_llm_trace_false_writes_nothing`; `test_the_prompt_is_not_saved_by_default_but_is_with_llm_trace_prompt`; `test_an_unwritable_trace_folder_does_not_fail_the_document` (make `llm/` a file); `test_the_trace_holds_no_secret` (settings with an API key string: absent from every trace file). Unit: `test_settings_llm_trace_defaults`.
- [ ] **Step 2: Run** `uv run pytest tests/integration/git/test_llm_traces_in_run.py tests/unit/test_config.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the settings, the `ask_llm` wiring and the `run.py` change.
- [ ] **Step 4: Run** `uv run pytest tests/unit tests/component tests/integration -q`. Expected: PASS, Stage A tests unchanged (the existing run tests must not see extra uncommitted files: the trace files are committed in the same run commit).
- [ ] **Step 5: Commit** `feat: every LLM call leaves a trace in the idea-bucket`.

### Task 4: read the saved reply first, and `--refresh-llm`

**Files:**
- Modify: `src/catcher/modules/llm/service.py` (`Replayer` gets the prompt version; a replayed result says `backend="saved"`; the recorder is NOT called for a replayed call), `src/catcher/modules/llm/trace.py` (`find` rules, `put` fake-protection), `src/catcher/core/config.py` (`llm_cache: bool = True`), `src/catcher/modules/pipeline/process.py` (`ProcessOptions.refresh_llm: bool = False`; `ask_llm` wires the replayer), `src/catcher/modules/pipeline/run.py` (`RunOptions.refresh_llm`, passed on), `src/catcher/cli.py` (`--refresh-llm` on `catcher run pipeline`), `.env.example`
- Test: `tests/component/test_llm_trace_hooks.py` and `tests/component/test_llm_trace_store.py` (adjust the tests of Tasks 1-2 only where the new signature requires it, and add new ones), `tests/integration/git/test_llm_cache.py` (new)

**Interfaces:**
- Produces: `Replayer = Callable[[LlmRequest, str, str], list[str] | None]` (request, `content_key`, `prompt_version`); `TraceStore.find(task: str, content_key: str, *, profile: str, prompt_version: str) -> list[str] | None` (only traces with `outcome == "ok"` whose `task`, `profile`, `prompt_version` and `content_key` match; newest `saved_at` parsed as an aware datetime wins; None, never `[]`); `TraceStore.replayer()` returns the matching `Replayer`. `ask_llm` passes `replayer=` (and no recorder) when `llm_dir` is set, `settings.llm_cache` is true and `refresh_llm` is false; a dry run READS saved replies too (and still writes nothing). A replayed `LlmResult` **mirrors the recording** (ruling 2026-10-02, so a page from a saved reply is byte-identical to the live page): `backend`, `model`, tokens and duration come from the saved trace (`SavedReply` dataclass: `replies`, `backend`, `model`, `tokens_in`, `tokens_out`, `duration_ms`; `Replayer` returns `SavedReply | None`, `find` returns `SavedReply | None`), with a new `LlmResult.from_saved: bool = False` set True; the run logs `"<label>: using the saved LLM reply (no call)"` when `from_saved`. With `refresh_llm` (or no saved reply) the model is called and the recorder writes as in Task 3.
- `TraceStore.put` additionally never replaces a trace whose backend is not `fake` and that holds replies with a trace whose backend is `fake` (a `--profile fake` run must not overwrite a paid reply); the keep-old log is a warning.
- A reply that produced an **invalid page** is not a good reply (ruling 2026-10-02): after `run.py` files a note as failed because page validation failed (not in a dry run) it calls `TraceStore.mark_unusable(target_rel, reason=...)`, which rewrites an `ok` trace with `outcome="invalid_page"` (never reused; a plain requeue calls the model again and its new trace overwrites the file). The Stage A test `test_requeue_of_a_failed_note_clears_failed_and_its_error_file` stays unchanged.
- CLI: `catcher run pipeline --refresh-llm` ("call the LLM again even when a good reply is saved in llm/"); `--requeue` is unchanged and may be combined with it.

- [ ] **Step 1: Write the failing test.** Component: `test_find_only_returns_ok_traces`; `test_find_needs_the_same_profile_and_prompt_version`; `test_find_never_returns_an_empty_list`; `test_find_picks_the_newest_by_parsed_time_not_by_string` (mixed UTC offsets); `test_a_fake_trace_never_replaces_a_real_one`; `test_a_replayed_result_says_saved_and_calls_no_recorder`; hooks tests adjusted for the new `Replayer` signature. Integration (real git repos, fake backends, the seed helper of `test_llm_traces_in_run.py` copied into the new file): `test_a_second_run_reuses_the_saved_reply_and_never_calls_the_backend` (run, requeue, backend that raises if built: identical page, trace file bytes unchanged); `test_requeue_alone_reuses_the_saved_reply` (the user's decision: nothing implicit); `test_refresh_llm_calls_the_model_again_and_overwrites_the_trace`; `test_requeue_with_refresh_llm_calls_the_model_again`; `test_deleting_the_trace_file_gives_a_fresh_call`; `test_a_prompt_version_change_is_a_miss` (monkeypatch the rendered version); `test_another_profile_is_a_miss`; `test_an_invalid_output_trace_is_not_reused` (garbage twice, then a good backend: called, page published); `test_llm_cache_false_always_calls`; `test_a_dry_run_reads_saved_replies_and_writes_nothing`; `test_the_saved_reply_is_validated_again` (hand-edit a saved reply to be invalid: same failure as live, backend called when `ok` trace is edited? decide: an `ok` trace whose reply no longer validates falls through to the backend with a warning); `test_a_new_tag_order_applies_to_a_saved_reply` (the cached reply holds two idea types; the page gets the higher-priority one).
- [ ] **Step 2: Run** `uv run pytest tests/component/test_llm_trace_hooks.py tests/component/test_llm_trace_store.py tests/integration/git/test_llm_cache.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the changes above; keep `reason()` without hooks identical (the Stage A tests are the net).
- [ ] **Step 4: Run** `uv run pytest tests/unit tests/component tests/integration -q`. Expected: PASS (the known `test_testdata_run` failure is fixed in Task 5).
- [ ] **Step 5: Commit** `feat: read the saved LLM reply before calling the model, and --refresh-llm`.

### Task 5: freeze the real run as the test data

**Files:**
- Create (copied from the user's finished real run in `tmp/ic`, never from a fresh run): `tests/data/idea-bucket/facts/*.json`, `tests/data/idea-bucket/llm/**/*.json`, `tests/data/expected/**` (the published pages of `tmp/ic/epiaku-docs/hugo/content/en/docs/idea-bucket/` for the 43 documents)
- Modify: `tests/integration/git/test_testdata_run.py` (rewrite for the new data; the old test expected a duplicate chat and stalled YouTube clips that this data no longer has), `docs/idea-catcher-how-to-run.md` (the "Recipes on the test data" section: what the folders hold)
- Test: `tests/integration/git/test_testdata_run.py`, `tests/component/test_testdata.py` (must still pass unchanged)

**Interfaces:**
- Consumes: Task 4's saved-reply reading and the existing saved-facts reading. `catcher testdata reset` copies `tests/data/idea-bucket` (now with `facts/` and `llm/`) so a reset repo starts with them.
- Produces: `test_the_whole_pipeline_on_the_committed_test_data`: `reset_test_repos` into a tmp folder, `run_pipeline` with real profile names (`make_services` backends that **raise if called**) and a YouTube fetcher that raises: 43 `published`, 1 `artifact`, 0 failed/deferred/unreadable, nothing left in `inbox/`, both git repos clean, every `llm/` trace untouched (bytes) and no new trace file, and each published page equals its file in `tests/data/expected/` **matched by the frontmatter `original_filename`** (not by `id`: a note's id is drawn again on every scan, so it differs between runs) after dropping the volatile frontmatter keys (calculated names, the note `id`, timestamps): this proves the whole pipeline runs on real-looking data with no model and no YouTube call.

- [ ] **Step 1: Write the failing test** (the rewrite above; it fails until the data exists).
- [ ] **Step 2: Copy the data** from `tmp/ic` (the user has ALREADY copied `facts/` and `llm/` into `tests/data/idea-bucket/` by hand (untracked, 2026-10-02 22:16): verify they are identical to `tmp/ic/idea-bucket/facts` and `llm` with `diff -r`, and only add what is missing: the expected pages) (if `tmp/ic` is not there, stop and report: do not generate it). Do not copy `tmp/ic/idea-bucket/inbox` (the inbox already lives in `tests/data` and is the user's); do not edit anything under `tests/data/idea-bucket/inbox`.
- [ ] **Step 3: Run** `uv run pytest tests/integration/git/test_testdata_run.py tests/component/test_testdata.py -q` and fix only the new test's normalisation (never the data).
- [ ] **Step 4: Run** `uv run pytest tests/unit tests/component tests/integration -q` and the whole suite with the network-blocking plugin from the scratchpad (`PYTHONPATH=<scratchpad> uv run pytest tests -p blocknet -q`): 0 blocked attempts. Expected: PASS.
- [ ] **Step 5: Commit** `test: freeze the real run (facts, LLM replies, expected pages) as the test data`.

### Task 6: docs, the layout and the cleanup backlog

**Files:**
- Modify: `docs/idea-catcher-how-to-run.md` (a short "Saved LLM replies" section: what `llm/<subfolder>/<name>.json` holds, where it is, that a run **reads a good saved reply before calling the model** like it reads saved facts, `LLM_CACHE`, `LLM_TRACE`, `LLM_TRACE_PROMPT`, `--refresh-llm` (also with `--requeue`), delete the file or folder for a fresh answer, the key (task, profile, prompt version, document text), that a glossary/context change needs a refresh, `catcher render`/`reason` write and read nothing, `duration_ms` is the last HTTP call only, commit and push after a real run, how to read a trace for debugging), `docs/idea-catcher-configuration.md` (`LLM_CACHE`, `LLM_TRACE`, `LLM_TRACE_PROMPT`), `docs/idea-catcher-pipeline.md` (the repo layout: `llm/` next to `facts/`), `docs/idea-catcher-service-architecture.md` (a backlog item: **`catcher cleanup`** for old `facts/`, `llm/`, `archive/` and `output/` records, dry run first, by age and status, with the warning that deleting saved facts causes a YouTube refetch and deleting saved replies costs LLM calls; and the line in the LLM section), `.env.example`, `README.md` (flag table: `--refresh-llm`)

- [ ] **Step 1: Verify** by hand with the test data: `uv run catcher testdata reset`, `uv run catcher run pipeline --ideas tmp/ic/idea-bucket --docs tmp/ic/epiaku-docs` with the fake backends is NOT possible through the CLI (profiles are real); instead verify with a throw-away script or the test of Task 5 and show the output of one run using `--profile fake` on an EMPTY copy for the trace layout only. Say in the docs that a reset repo needs no model and no YouTube call.
- [ ] **Step 2: Write** the doc changes, proportionate, matching the existing tone; no front matter changes.
- [ ] **Step 3: Run** `uv run pytest tests/unit tests/component tests/integration -q`. Expected: PASS.
- [ ] **Step 4: Commit** `docs: saved LLM replies, --refresh-llm and the cleanup backlog`.
