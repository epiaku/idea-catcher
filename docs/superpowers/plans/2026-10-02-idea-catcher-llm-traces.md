# Idea Catcher: keep the raw LLM reply (LLM traces) and replay it

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every LLM call leaves a trace file next to the other repo state (like `facts/<video id>.json` does for YouTube): the raw replies, which model and prompt version answered, and the validated output. The same files can later be **replayed** instead of calling a model, so a frozen real run becomes a mock for tests and a record to debug a result.

**Architecture:** `reason()` (the one LLM function) gets two optional hooks: a `recorder` that receives an `LlmTrace` after the call, and a `replayer` that supplies the raw replies instead of the backend (the replies still go through the same JSON parsing, validation and retry code). A `TraceStore` writes and finds `llm/<subfolder>/<calculated name>.json` in the idea-bucket repo. `ask_llm` and `run_pipeline` wire it in; the files are committed with the run, like the saved YouTube facts.

**Tech Stack:** Python 3.12, uv, Pydantic v2, pytest. No database, no network.

**Spec:** this plan is the spec for a new feature; related design: `docs/idea-catcher-service-architecture.md` (LLM Step & Profiles; the saved facts) and `docs/idea-catcher-youtube-bans-and-queue-options.md` (saved facts as the model).

## Decisions taken (defaults; veto any before execution)

1. **Always on:** `LLM_TRACE=true` by default. Set `LLM_TRACE=false` to switch it off.
2. **Where:** `llm/<subfolder>/<calculated name>.json` at the root of the idea-bucket repo, the same subfolder and calculated name as `archive/`, `output/` and `failed/` (`note.target_rel` with `.json`). A requeue overwrites the same file.
3. **What is in it:** `version` (1), `saved_at`, `task`, `profile`, `backend`, `model`, `prompt_version`, `content_key`, `prompt_sha256`, `attempts` (a list: `reply`, `tokens_in`, `tokens_out`, `duration_ms`, `model`, `error`), `outcome` (`ok`, `invalid_output`, `backend_error`), `error`, `output` (the validated JSON, when ok). **The prompt text itself is not saved** unless `LLM_TRACE_PROMPT=true` (a prompt holds the whole transcript and can be 100 KB; the hash plus the prompt version identify it).
4. **Replay key:** `content_key = sha256(task + NUL + body + NUL + transcript_or_empty)`: the document text, not the document id (a note's id is random per scan) and not the prompt (editing a prompt must not break the replays). So replay tests the flow, not prompt quality.
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
2. **Replay is exact:** replaying a recording run gives byte-identical pages, and a backend that raises proves no model was called (Task 4).
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

### Task 4: replay through `Services`

**Files:**
- Modify: `src/catcher/modules/pipeline/process.py` (`Services.llm_replay: TraceStore | None = None`; `ask_llm` passes `replayer=svc.llm_replay.replayer()` when set)
- Test: `tests/integration/git/test_llm_replay.py`

**Interfaces:**
- Produces: with `Services.llm_replay` set, a run uses the recorded replies and never builds a backend; a document with no matching trace calls the backend as before (a missing recording is not an error, so a test that wants to be strict passes a backend factory that raises).

**Requirements added by the final review of Tasks 1-3 (must be part of this task):** (a) a **strict mode** `Services.llm_replay_strict: bool = False`: when True a replay miss raises `InvalidOutput("no recording for <task> <content_key>")` instead of silently calling the paid backend (today a miss falls back to the backend); (b) `TraceStore.find` skips traces whose `backend` is `fake` or `replay` (a `--profile fake` trace must never win as "newest"); (c) an interrupted call (Ctrl-C/SystemExit after an attempt started) is recorded with `outcome` `backend_error` and `error="interrupted"`, and `find` accepts a trace whose attempts hold replies even when the outcome is not `ok`/`invalid_output` only when the replies validated: keep it simple: skip `backend_error`, but never overwrite a trace holding replies (done in the fix wave); (d) `saved_at` is parsed as an aware datetime when choosing the newest. (e) the store's keep-old rule (`put`) overwrites an `ok` trace with a `backend_error` trace that holds one valid-to-store reply (attempt 1 replied, attempt 2 raised), and `find` skips `backend_error` traces, so the replayable old replies are replaced: make `put` keep an old trace that `find` could use (`ok`/`invalid_output` with replies) when the new one is `backend_error`.

- [ ] **Step 1: Write the failing test.** `test_replay_gives_identical_pages_and_calls_no_backend` (Review Focus #2: run 1 with the fake backends and traces written; run 2 on a fresh copy of the same seed with `llm_replay=TraceStore(run1_ideas / "llm")` and backends that raise if built: the pages in epiaku-docs and the `output/` files are identical after normalising the random calculated names and `analyzed_at`); `test_a_document_without_a_recording_falls_back_to_the_backend`; `test_replay_of_an_invalid_then_valid_recording_reproduces_the_retry` (a recording with two attempts replays both and gives the same `attempts` in the result); `test_replay_still_validates` (a hand-edited recording with invalid JSON gives the same failure as live).
- [ ] **Step 2: Run** `uv run pytest tests/integration/git/test_llm_replay.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the field and the call.
- [ ] **Step 4: Run** `uv run pytest tests/unit tests/component tests/integration -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: replay recorded LLM replies through Services`.

### Task 5: docs, the layout and the cleanup backlog

**Files:**
- Modify: `docs/idea-catcher-how-to-run.md` (a short "LLM traces" section: what the file holds, where it is, `LLM_TRACE`/`LLM_TRACE_PROMPT`, how to read one for debugging, how a frozen run becomes a test mock: copy `idea-bucket/inbox` (input), `facts/`, `llm/` and the outputs into `tests/data/...`, and that replay is keyed by the document text), `docs/idea-catcher-configuration.md` (the two settings), `docs/idea-catcher-pipeline.md` (the repo layout: the `llm/` folder next to `facts/`), `docs/idea-catcher-service-architecture.md` (a backlog item: **`catcher cleanup`** for old `facts/`, `llm/`, `archive/` and `output/` records, dry run first, by age and status, with the warning that deleting saved facts causes a YouTube refetch on a requeue; and a line in the LLM section), `.env.example`, `README.md` (if the folder list is there)

**Notes from the final review that the docs must state:** the replay key (`content_key`) is the document body plus the transcript and ignores the title hint, the source URL, tags and facts metadata, so two documents with the same body get the same replay; editing a body by hand or `--refresh-facts` (new captions) changes the key and causes a miss; `catcher render` and `catcher reason` write **no** trace (only `catcher run pipeline` does); `duration_ms` is the last HTTP call only, not the transient retries; do not run `--profile fake` against a real set (fake traces are skipped by `find`, but keep the real set clean); a failed retry never overwrites a trace that holds replies; the trace file is committed with the run, so commit and push after a real run.

- [ ] **Step 1: Verify** by hand-reading a trace written by the Stage A test data run with the fake backend: `uv run catcher testdata reset`, `uv run catcher run pipeline --ideas tmp/ic/idea-bucket --docs tmp/ic/epiaku-docs --profile fake`, then look at `tmp/ic/idea-bucket/llm/`. Put one trimmed example (fake backend) in the how-to.
- [ ] **Step 2: Write** the doc changes, proportionate, matching the existing tone; no front matter changes.
- [ ] **Step 3: Run** `uv run pytest tests/unit tests/component tests/integration -q`. Expected: PASS.
- [ ] **Step 4: Commit** `docs: LLM traces, replay and the cleanup backlog`.
