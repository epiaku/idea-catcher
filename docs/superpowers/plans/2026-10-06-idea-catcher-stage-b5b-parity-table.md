# Stage B5b: the parity table for retiring the old pipeline loop

Task 1 of `2026-10-06-idea-catcher-stage-b5b-run-pipeline-over-the-worker.md`. This table is the checklist for
deleting `run_pipeline()` (`src/catcher/modules/pipeline/run.py`): every behaviour that a test of the old loop
pins is listed once, with the worker test that already pins it, or `GAP`.

How to read it:

- **One row per behaviour.** A test that pins several behaviours is listed in several rows.
- **proposed action:**
  - `covered`: a worker test (or a component test of the code the worker also runs) already pins it. The old
    test still moves to `run_on_worker(...)` (default 8), but nothing new has to be built.
  - `add to worker: <what>`: no worker test pins it yet, or the worker does not do it yet. The ported test is
    the first pin; `<what>` says if code is needed too. Dry-run rows say `port to the preview`: decision 12
    keeps `--dry-run` as a read-only preview (`pipeline/preview.py`, Task 4), not a worker job.
  - `drop on purpose: <reason>`: the old assertion cannot hold on the worker path. The user decides (see
    "Decisions for the user").
- **effort:** S (a test port, maybe a few lines of code), M (new code in a handler or the command, plus
  tests), L (new data, a new builder and many tests depend on it).
- **Changed** in a cell: the behaviour stays, but an exact old assertion (a message, a folder, a count)
  changes on the worker path; the cell says how.

File keys used in the table (old loop tests left, worker tests right):

| key | file | key | file |
|---|---|---|---|
| `run` | tests/integration/git/test_run.py | `hrun` | tests/integration/db/test_handler_pipeline_run.py |
| `traces` | tests/integration/git/test_llm_traces_in_run.py | `hllm` | tests/integration/db/test_handler_llm_reason.py |
| `cache` | tests/integration/git/test_llm_cache.py | `hyt` | tests/integration/db/test_handler_youtube_fetch.py |
| `saved` | tests/integration/git/test_llm_saved_flag.py | `hpub` | tests/integration/db/test_handler_publish.py |
| `testdata` | tests/integration/git/test_testdata_run.py | `loop` | tests/integration/db/test_worker_loop.py |
| `paths` | tests/unit/test_missing_paths.py | `wcli` | tests/integration/db/test_worker_cli.py |
| `cli` | tests/unit/test_cli.py | `crash` | tests/integration/db/test_worker_crashes.py |
| `e2e` | tests/integration/db/test_worker_end_to_end.py | `rcli` | tests/integration/db/test_run_pipeline_cli.py |
| | | `blocks` | tests/integration/db/test_backend_blocks.py |
| | | `stuck` | tests/integration/db/test_stuck.py |
| | | `c-…` | a component test: `c-inbox`, `c-publish`, `c-store` (test_llm_trace_store), `c-hooks` (test_llm_trace_hooks), `c-process` |

## The table

### A. Naming, folders, commit and push

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B1 | A note and a chat are published: calculated names (date, id, slug), one name in `archive/`, `output/` and the docs page; the archive copy keeps the body and gets `original_filename`/`calculated_filename`; `output/` says `stage: published` and `source_file`; `inbox/` is empty; no `staging/` folder | run::test_run_publishes_archives_and_commits | hrun::test_run_stages_a_note_and_enqueues_llm_reason (names in archive/output); hllm::test_llm_reason_publishes_a_note_page_and_marks_the_item_published; hllm::test_a_top_level_and_a_nested_capture_publish_through_the_worker; e2e golden pages; c-inbox naming tests | covered | S |
| B2 | The run commits both repos (clean `git status`), commits only its own folders (a user edit of `README.md` stays uncommitted) and pushes nothing without `--push` | run::test_run_publishes_archives_and_commits; run::test_unrelated_docs_changes_are_not_committed | `pipeline.publish`: hpub::test_publish_without_pull_or_push_commits_locally_only; hpub::test_unrelated_docs_changes_are_not_committed; hpub::test_the_docs_commit_set_is_the_three_destination_folders_and_the_artifacts. **Changed:** publish commits the managed folders as a whole, not only the files this run touched | covered | S |
| B3 | The commit message carries the counts: `idea-catcher: publish 2 page(s)`, `... and 1 artifact(s)`, `idea-catcher: process the inbox (N published)` | run::test_run_publishes_archives_and_commits; run::test_an_artifact_is_archived_and_copied_to_epiaku_docs_and_committed | GAP: publish uses fixed messages (`idea-catcher: publish pages (pipeline.publish)`, `idea-catcher: process the inbox (pipeline.publish)`) | drop on purpose: a publish job commits whatever the managed folders hold, possibly the work of several runs and of the worker; a count from one run would be wrong. The ported tests assert the fixed messages | S |
| B4 | `--push` pushes both repos | run::test_push_updates_both_remotes | hpub::test_publish_commits_both_repos_and_pushes_to_the_remotes | covered | S |
| B5 | With `--push` the run pulls both repos first; a failed pull is a reported problem and nothing is changed | run::test_a_failed_pull_is_a_reported_problem_and_nothing_is_changed | publish commits first, then pulls with a rebase, then pushes: hpub::test_a_failed_pull_fails_the_job_and_leaves_the_commit_on_the_branch; hpub::test_a_conflicting_remote_edit_in_ideas_aborts_the_rebase_and_fails | drop on purpose: the pull now comes after the work (the work is done and committed locally; a failed pull fails the publish job and the next publish retries). "Nothing is changed" no longer holds | S |
| B6 | A failed push is a reported problem after the work is committed (`committed` true, `pushed` false) | run::test_a_failed_push_is_a_reported_problem_after_the_work_is_committed | hpub::test_a_rerun_pushes_a_commit_that_a_failed_push_left_behind; hpub::test_a_rejected_push_whose_rebase_conflicts_is_aborted. The command must turn the publish job's `Fail` into a problem line (Task 3) | covered | S |
| B7 | The saved YouTube facts and the LLM traces are committed with the run | run::test_the_saved_facts_are_committed_with_the_run; traces::test_a_run_writes_a_trace_per_document_and_commits_it | `facts/` and `llm/` are in `IDEAS_MANAGED`: hpub::test_publish_twice_makes_no_second_commit (facts), hpub::test_a_rerun_pushes_a_commit_that_a_failed_push_left_behind (llm) | covered | S |

### B. The outcome of one document

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B8 | Invalid LLM output: the note goes to `failed/` with the reason in `.error.txt`, keeps its archive copy (same name, same text), leaves no working copy; the other note publishes | run::test_invalid_output_moves_the_note_to_failed_with_the_reason; run::test_a_failed_note_keeps_its_archive_copy_and_leaves_no_working_copy | hllm::test_invalid_output_moves_the_note_to_failed | covered | S |
| B9 | An unexpected error fails that note, is logged at ERROR with a traceback, and the run goes on | run::test_an_unexpected_error_fails_that_note_and_is_logged_with_a_traceback | hllm::test_an_unexpected_error_fails_the_item_and_the_job; hyt::test_an_unexpected_error_fails_the_item_and_the_job. **Changed:** the job fails too; the patch target moves to `handlers_pipeline.process_note`. The traceback (`exc_info`) is not asserted | add to worker: assert the ERROR record carries `exc_info` | S |
| B10 | An unreadable capture is archived and filed in `failed/` (bytes unchanged, `original file:` in `.error.txt`) and reported in `report.unreadable` | run::test_an_unreadable_capture_is_reported_and_filed_as_failed | files: hrun::test_duplicates_unreadable_and_artifacts_are_handled_like_stage_a. The names and reasons: only a count (`unreadable`) in the job result, see B72 | covered (files); the report part is B72 | S |
| B11 | A capture with a broken `source:` line goes to `failed/` (`cannot analyse:`) and the run goes on | run::test_a_capture_with_a_broken_source_line_is_filed_as_failed_and_the_run_goes_on | hrun::test_a_capture_with_a_broken_source_line_goes_to_failed_and_the_others_are_staged | covered | S |
| B12 | An empty document, and one over `LLM_MAX_INPUT_CHARS`, fail without an LLM call | run::test_an_empty_document_fails_without_an_llm_call; run::test_a_document_over_the_size_limit_fails_without_an_llm_call | GAP (`llm.reason` runs the same `process_note`; no worker test) | add to worker: port both | S |
| B13 | A published document, or a duplicate, is not processed again: a second run finds nothing | run::test_a_published_document_is_not_processed_again; run::test_duplicates_are_not_processed_again | hrun::test_running_twice_stages_nothing_twice | covered | S |
| B14 | Two clips with one id but different content are both processed, and a warning says the later page replaces the earlier one (log and report message) | run::test_two_clips_with_one_id_but_different_content_are_both_processed; run::test_two_different_clips_with_one_id_warn_that_the_later_page_replaces_the_earlier | GAP: `pipeline.run` stages both and says nothing (the old `RunState.seen_ids` check is gone) | add to worker: `pipeline.run` warns when two staged notes share an id; the warning goes into the run's result for the report (B72) | S |
| B15 | A chat clipped again later overwrites its page (one page per id); both clips stay in `archive/` | run::test_reclipped_chat_overwrites_its_page | c-publish::test_write_page_replaces_the_page_with_the_same_id (shared `write_page`); no worker test | add to worker: port | S |
| B16 | A profile without a model is a configuration error: the note is deferred (not failed) and an ERROR line says `configuration error` | run::test_a_missing_model_is_a_configuration_error_and_the_note_is_not_failed | GAP (same `classify`/`log_outcome`; no worker test) | add to worker: port | S |
| B17 | A manual retry (moving the archive copy back into `inbox/`) runs the document again under the same calculated name and overwrites the stalled copy | run::test_youtube_without_facts_is_deferred_then_published; run::test_a_usage_limit_stalls_the_note_in_output_until_you_move_it_back | `stage_item` resets a row in a final status: hrun::test_requeue_resets_the_existing_item (through `requeue`); the hand move is not tested | add to worker: port | S |

### C. LLM usage limits and budgets

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B18 | A usage limit defers every note of that backend in the run with one call; another backend's notes publish; a deferred note stays in `output/` with `stage: deferred` and the reason, out of `inbox/`, not in `failed/` | run::test_a_rate_limit_defers_every_note_of_that_backend_but_not_the_others; run::test_a_usage_limit_stalls_the_note_in_output_until_you_move_it_back | hllm::test_a_usage_limit_blocks_the_backend_and_the_next_document_makes_no_call; hllm::test_a_block_of_one_backend_leaves_the_other_backends_alone; hllm::test_a_backend_outage_defers_the_item_and_succeeds_the_job. **Changed:** the second note's reason is `<backend>: not called again until <time> (earlier: <cause>)`, not the first note's message | covered | S |
| B19 | A used-up budget: every waiting note says `budget reached (openai)`, and one ERROR line per backend says `openai budget reached: 2 note(s) waiting; raise the key's budget ...` | run::test_a_used_up_budget_logs_one_error_per_backend_and_keeps_the_notes | hllm::test_a_used_up_budget_blocks_the_backend_through_the_worker: the first note says `budget reached`, the next ones `not called again until ...`; no summary line | drop on purpose: the in-run per-backend counter (`RunState.budget_blocked`) and the identical message on every note; the Postgres block (`resources`) replaces them. See the decision for a cheap replacement line | S |
| B20 | The deferred notes go through once the budget is back (the next run) | run::test_the_waiting_notes_go_through_once_the_budget_is_back | blocks::test_a_budget_block_is_six_hours_and_a_usage_limit_ten_minutes; hllm::test_after_the_cool_down_one_probe_is_made_and_a_failure_blocks_again. **Changed:** the block outlives the run: a budget block lasts `LLM_BUDGET_BLOCK_S` (6 h) in Postgres; the port must move the clock past it | covered | S |

### D. Log lines

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B21 | Per-document progress lines `(1/2) <label>: processing` / `(2/2) ...: published`, the inbox path in the label, and a total `processed 2/2: 2 published` | run::test_run_logs_progress_per_note_and_a_total | the worker logs `inbox: N document(s) to process (catcher version X)`, `<label>: published <file>` per `llm.reason`, and `pipeline.run: {counts}`; the label names the calculated name (`source_file`), not `inbox/...` | drop on purpose: the documents run as separate jobs (maybe over several runs), so there is no position or total; the port asserts the worker's lines instead | S |
| B22 | Naming, archiving and the LLM steps are DEBUG lines, not INFO | run::test_the_steps_of_a_document_are_debug_lines_not_info | GAP (same functions; no worker test) | add to worker: port | S |
| B23 | A failure or a deferral is always logged (ERROR with the id and the reason) | run::test_failures_and_deferrals_are_always_logged | GAP (same `log_outcome`; no worker test). **Changed:** no `/2)` position in the line | add to worker: port without the position | S |
| B24 | A limit is logged once, not once per waiting note | run::test_a_limit_is_logged_once_not_for_every_note_that_waits | hrun::test_a_big_inbox_is_logged_in_two_lines_not_one_per_document. **Changed:** `document(s)` instead of `note(s)`, logger `catcher.worker.pipeline` | covered | S |
| B25 | The version is logged when a run starts | cli::test_the_version_comes_from_one_place_and_is_logged_when_a_run_starts | hrun::test_a_big_inbox_is_logged_in_two_lines_not_one_per_document (`catcher version X`). The test's run is a dry run: the preview must log it too (B55) | covered | S |

### E. Snapshots and duplicates

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B26 | Growing snapshots of one conversation cost one LLM call: the earlier ones go to `duplicates/` with `duplicate_of` and no `stage`, keep their archive copy (same name, same text, `original_filename`), get no working copy; one page per id; a log line names `duplicates/` and the winner | run::test_growing_snapshots_of_one_conversation_cost_one_llm_call; run::test_a_duplicate_keeps_its_archive_copy | hrun::test_duplicates_unreadable_and_artifacts_are_handled_like_stage_a (the move, `duplicate_of`, the archive name); c-inbox::test_move_to_duplicates_archives_moves_and_records_the_winner. Not pinned: one call over three snapshots, the body equality, the log line | add to worker: port (drain, count the prompts) | S |
| B27 | `--file` naming a shorter clip whose longer clip is not selected processes the shorter one | run::test_a_named_short_clip_is_processed_when_its_longer_clip_is_not_selected | GAP | add to worker: port | S |

### F. `--limit`

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B28 | `--limit N` works on at most N documents; the rest stay in `inbox/` untouched (no archive copy, no working copy) | run::test_limit_processes_at_most_n_notes; run::test_a_document_leaves_the_inbox_only_when_it_is_worked_on | hrun::test_limit_counts_the_staged_notes. **Changed:** it counts staged documents (Stage A counted the ones worked on, a waiting clip did not count; on the worker nothing waits at staging, so it is the same). The `skipped` lines are B72 | covered | S |
| B29 | `--limit` with `--file`; `--limit` does not apply to artifacts | run::test_file_and_limit_work_together; run::test_limit_does_not_apply_to_artifacts_and_file_can_select_only_one | GAP (the handler copies every artifact after the limit; no test) | add to worker: port | S |

### G. `--file`

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B30 | `--file` processes only the named document; the others stay untouched in `inbox/` | run::test_file_processes_only_the_named_document | hrun::test_run_stages_a_note_and_enqueues_llm_reason (`only`); hrun::test_retry_deferred_requeues_stalled_items ("not selected") | covered | S |
| B31 | A `--file` name that matches nothing: a WARNING (`no document named "..."`, only `inbox/` is searched, use `--requeue`), the name in `report.not_found`, nothing processed; together with an unknown `--requeue` name it is a warning, not a crash | run::test_file_that_finds_nothing_warns_and_processes_nothing; run::test_file_only_looks_in_the_inbox_never_in_output; paths::test_a_named_file_that_does_not_exist_is_a_warning_not_a_crash | GAP: `pipeline.run` neither warns nor records an unmatched `only` name (an unknown `requeue` name is only logged) | add to worker: `pipeline.run` warns as Stage A did and puts the unmatched names in its result (B72) | S |
| B32 | `--file` can name just an artifact | run::test_file_can_name_just_an_artifact | GAP (c-inbox::test_file_finds_an_artifact_by_its_original_name_too pins the scan only) | add to worker: port | S |

### H. Artifacts and the size limit

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B33 | An artifact is archived and copied to epiaku-docs `idea-bucket/artifacts/` under one name, out of `inbox/`, never into the Hugo content, and committed | run::test_an_artifact_is_archived_and_copied_to_epiaku_docs_and_committed | hrun::test_duplicates_unreadable_and_artifacts_are_handled_like_stage_a; hpub::test_the_docs_commit_set_is_the_three_destination_folders_and_the_artifacts; e2e::test_the_worker_publishes_the_frozen_real_run_with_no_external_call | covered (commit message: B3) | S |
| B34 | An artifact moved back into `inbox/` keeps its name and overwrites the same files | run::test_a_requeued_artifact_keeps_its_name | c-inbox::test_a_requeued_artifact_keeps_its_name_and_overwrites_the_same_files (shared `copy_artifact`) | covered | S |
| B35 | A file over `ARTIFACT_MAX_MB` is skipped with a WARNING and stays in `inbox/` | run::test_a_file_over_the_size_limit_is_skipped_with_a_warning_and_stays_in_the_inbox | GAP (same `copy_artifacts`, but its `skipped`/`failed` lines are thrown away: the result counts only `artifact`) | add to worker: port; the skipped artifact goes into the result (B72) | S |

### I. `--requeue`

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B36 | `--requeue <captured name>` brings an archived document back and runs only it; it keeps its calculated name and overwrites the stalled copy | run::test_requeue_brings_a_stalled_document_back_and_runs_it_again | hrun::test_requeue_resets_the_existing_item; hrun::test_requeue_resets_a_stuck_active_item_that_no_job_carries. The `requeued` line is B72 | covered | S |
| B37 | A requeue that stalls again leaves the document in one place (out of `inbox/`, archive and output written again) and everything committed | run::test_requeue_moves_the_original_and_clears_the_stale_output_so_the_document_is_in_one_place | GAP (the pieces are pinned: requeue, `youtube.fetch` defers on no facts, publish commits) | add to worker: port | S |
| B38 | `--requeue <folder>/<calculated name>` reruns a published note with the same page; the other document is not touched | run::test_requeue_by_calculated_name_reruns_a_published_note_with_the_same_page | GAP for the path form (c-inbox::test_a_document_can_be_named_in_several_ways pins the matching) | add to worker: port | S |
| B39 | An unknown `--requeue` name is reported in `not_in_archive` and nothing runs | run::test_requeue_of_an_unknown_name_is_not_found_and_runs_nothing | logged only (`_requeue` warns); not in the result | add to worker: the unmatched names go into the result (B72) | S |
| B40 | A requeue never overwrites a file already in `inbox/` (reported `skipped ... already exists`); the edited inbox file is used | run::test_requeue_does_not_overwrite_a_file_already_in_the_inbox | GAP (shared `requeue_from_archive`; the `skipped` line is not recorded) | add to worker: port; the skipped name goes into the result (B72) | S |
| B41 | A requeue of a failed note clears `failed/` and its `.error.txt` | run::test_requeue_of_a_failed_note_clears_failed_and_its_error_file | GAP (shared `requeue_from_archive`) | add to worker: port | S |

### J. `--retry-deferred`

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B42 | A deferred document is not retried by a plain run; `--retry-deferred` puts it back and it publishes | run::test_retry_deferred_puts_the_stalled_documents_back_so_they_run_again | hrun::test_retry_deferred_requeues_stalled_items; stuck::test_retry_deferred_picks_up_stuck_items_too_and_a_publish_ends_it; e2e::test_an_llm_failure_makes_no_youtube_call. **Changed:** the database status is the truth, not the frontmatter in `output/` | covered | S |
| B43 | `--retry-deferred` with nothing stalled does nothing | run::test_retry_deferred_with_nothing_stalled_does_nothing | GAP | add to worker: port | S |

### K. YouTube

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B44 | A clip without facts (no transcript) is deferred and stays in `output/` (not `inbox/`, not `failed/`) | run::test_youtube_without_facts_is_deferred_then_published | hyt::test_no_transcript_defers_the_item_not_the_job | covered | S |
| B45 | A second clip inside the gap between YouTube calls waits; the next run after the gap takes it; the report says `waiting`, `next call allowed at ...` | run::test_a_second_clip_inside_the_gap_waits_in_the_inbox_and_the_next_run_takes_it | hyt::test_a_closed_gate_keeps_the_job_queued_until_the_next_slot; e2e::test_a_closed_gate_waits_in_the_queue_and_the_next_slot_publishes. **Changed:** the clip is staged (in `output/`, item `waiting_youtube`, a queued fetch job with `run_after`), not left in `inbox/` | covered | S |
| B46 | A gate that closes between the check and the start sends the clip back to `inbox/` with no call | run::test_a_gate_that_closes_after_the_check_sends_the_clip_back_to_the_inbox | hyt::test_a_fetch_job_claimed_in_a_race_defers_to_the_slot_without_an_attempt. **Changed:** there is no pre-check any more; the fetch job defers to the gate's time, the item keeps waiting in `output/` | covered | S |
| B47 | A 429 opens the breaker: every other clip waits with no call, nothing is stalled in `output/` as deferred, and hours later the breaker still holds | run::test_a_429_opens_the_breaker_and_every_other_clip_waits_without_a_call | hyt::test_a_429_opens_the_breaker_and_defers_without_counting_an_attempt; loop::test_a_worker_with_only_waiting_fetch_jobs_is_idle_and_counts_no_attempts | covered | S |
| B48 | A requeue or a rerun never asks YouTube again (saved facts) | run::test_a_requeue_or_a_rerun_never_asks_youtube_again | hyt::test_saved_facts_mean_no_second_call; hrun::test_a_youtube_clip_with_saved_facts_goes_straight_to_llm_reason; e2e::test_an_llm_failure_makes_no_youtube_call | covered | S |
| B49 | `--refresh-facts` asks YouTube again even when the facts are saved | run::test_refresh_facts_asks_youtube_again | GAP: no `refresh_facts` param in `pipeline.run`, `youtube.fetch` or `llm.reason` | add to worker: default 6, a `refresh_facts` param on `pipeline.run`, copied to the fetch job (queued even when facts are saved); the fetch passes it to `get_facts` | M |
| B50 | `--wait-youtube` sleeps through a short gap so one run does both clips; a wait longer than `YOUTUBE_WAIT_MAX_S` still leaves the clip waiting | run::test_wait_youtube_sleeps_through_the_gap_so_one_run_does_both_clips; run::test_wait_youtube_still_defers_a_wait_longer_than_the_limit | GAP (the fetch handler never sleeps) | add to worker: default 5, the command keeps polling while a fetch job of this run is due within `YOUTUBE_WAIT_MAX_S`. **Changed:** the `sleeps` the tests count become the command's poll sleeps | M |
| B51 | Notes and chats never wait for YouTube | run::test_notes_and_chats_never_wait_for_youtube | hrun::test_run_stages_a_note_and_enqueues_llm_reason; hrun::test_a_youtube_clip_is_staged_waiting_youtube_with_a_fetch_job | covered | S |

### L. `--dry-run` (the preview, Task 4)

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B52 | A dry run changes no file and commits nothing; it reports `would_publish`, `would_copy` (artifact), `would_requeue` and `duplicate` | run::test_dry_run_touches_nothing; run::test_dry_run_reports_duplicates_without_touching_files; run::test_a_dry_run_moves_nothing_out_of_the_inbox; run::test_a_dry_run_only_reports_the_artifact; run::test_requeue_dry_run_copies_nothing; run::test_requeue_dry_run_and_a_blocked_name_leave_archive_and_output_alone | not a worker job (decision 12) | add to worker: port to the preview (`pipeline/preview.py`) | M |
| B53 | A dry run never asks YouTube, saves no facts, leaves the gate untouched, reports `would_fetch` | run::test_a_dry_run_saves_no_facts | not a worker job | add to worker: port to the preview | S |
| B54 | A dry run reads the saved replies, writes no trace and marks none; a saved reply that makes an invalid page is reported `failed` | traces::test_a_dry_run_writes_no_trace; cache::test_a_dry_run_reads_saved_replies_and_writes_nothing; cache::test_a_dry_run_marks_no_trace | not a worker job | add to worker: port to the preview | S |
| B55 | A dry run does not need the docs folder, and logs the version when it starts | paths::test_a_dry_run_does_not_need_the_docs_folder; cli::test_the_version_comes_from_one_place_and_is_logged_when_a_run_starts | not a worker job | add to worker: port to the preview | S |

### M. Interrupts and the run lock

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B56 | Ctrl-C (or SIGTERM) in the middle of a document puts that document back in `inbox/`, reports `interrupted` and a problem, still commits what was done; the next run finishes it, nothing is archived twice | run::test_ctrl_c_in_the_middle_of_a_note_puts_it_back_in_the_inbox_and_still_commits | loop::test_keyboard_interrupt_and_system_exit_propagate; loop::test_the_reaper_requeues_a_crashed_job_at_start; crash::test_a_worker_killed_mid_llm_reason_is_recovered. **Changed:** the document stays staged in `output/` (item active, its job `running`), not back in `inbox/`; the job is reaped only after its lease (120 s), so a run started at once would not pick it up | add to worker: the command catches Ctrl-C, puts its own running job back to `queued` (it holds the lock, nobody else runs it), and reports the jobs left; whether it still publishes is a decision | M |
| B57 | A lost run lock stops the run before the next document, before the artifacts, and before the commit; nothing is committed; the report shows what was done | run::test_a_lock_lost_during_a_document_stops_the_run_before_the_next_one; run::test_a_lock_lost_after_the_last_document_stops_the_run_before_the_artifacts; run::test_a_lock_lost_after_the_artifacts_stops_the_run_before_the_commit | loop::test_run_once_checks_the_worker_lock_before_it_claims; wcli::test_a_worker_that_loses_its_lock_stops_with_exit_1; rcli::test_a_run_that_loses_its_lock_stops_with_exit_1_before_the_next_document (today on the old loop). **Changed:** the check is per job, not per document; `pipeline.run` stages all documents and copies the artifacts in one job, so "before the artifacts" no longer exists | add to worker: port "lock lost between jobs: no further job, no publish, nothing committed, exit 1"; the artifact checkpoint is dropped | M |

### N. Paths and the command

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B58 | A missing `inbox/` or docs folder is a problem (`... not found at ...: check --ideas/--docs or IDEAS_REPO/DOCS_REPO`), logged at ERROR, nothing touched, no commit | paths::test_run_pipeline_with_a_missing_ideas_folder_reports_a_problem_and_touches_nothing; paths::test_run_pipeline_with_a_missing_docs_folder_reports_a_problem (and paths::test_the_pipeline_command_with_wrong_paths_exits_2_and_says_which_path, which calls the command) | `pipeline.run` returns `Fail("... not found at ...")` without the hint; no test | add to worker: the command checks both paths before it queues anything (exit 2, no job), as `reconcile` does; port the tests | S |
| B59 | The command: exit 0 with one line per document and `summary:`; exit 1 with `not-found ... in inbox/` / `in archive/`; `--requeue`, `--retry-deferred` and `--refresh-llm` reach the run; `(saved reply)` after the page of a reused reply | run::test_cli_run_pipeline (calls the command, not `run_pipeline(`); run::test_cli_file_option_warns_and_exits_with_1_when_nothing_matches; run::test_requeue_flag_on_the_command_line; run::test_retry_deferred_flag_on_the_command_line; cache::test_requeue_with_refresh_llm_calls_the_model_again (its first half patches `cli.run_pipeline`); saved::test_the_command_line_marks_only_the_item_served_from_a_saved_reply | rcli (lock, database down, exit 2); wcli (`jobs add`, `worker --once`); hrun::test_profile_and_refresh_llm_are_copied_into_the_llm_reason_and_fetch_params | add to worker: Task 3 (the command over the worker); the `--refresh-llm` test reads the queued `pipeline.run` job's params instead of patching `run_pipeline` | M |

### O. LLM traces and saved replies

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B60 | One trace per document in `llm/`, named like the archive and output copy, overwritten (same file) on a requeue with `--refresh-llm`; a plain rerun writes nothing | traces::test_a_run_writes_a_trace_per_document_and_commits_it; traces::test_the_trace_name_matches_the_archive_and_output_name; traces::test_a_requeue_overwrites_the_same_trace_file | hllm::test_the_trace_file_is_written_next_to_the_page; hllm::test_refresh_llm_in_the_job_params_calls_the_model_again; c-store::test_put_overwrites_the_same_file_on_a_requeue | covered | S |
| B61 | A failed call still leaves a trace with the error (`invalid_output` with both replies; `backend_error` with no attempts), committed | traces::test_a_failed_llm_call_still_leaves_a_trace_with_the_error | c-hooks::test_two_invalid_replies_record_invalid_output_and_still_raise; c-hooks::test_a_backend_error_records_backend_error_and_propagates; no worker test | add to worker: port | S |
| B62 | `LLM_TRACE=false` writes no `llm/`; the prompt is kept only with `LLM_TRACE_PROMPT`; no API key in a trace; an unwritable `llm/` does not fail the document (a WARNING per document) | traces::test_llm_trace_false_writes_nothing; traces::test_the_prompt_is_not_saved_by_default_but_is_with_llm_trace_prompt; traces::test_the_trace_holds_no_secret; traces::test_an_unwritable_trace_folder_does_not_fail_the_document | c-hooks::test_the_prompt_is_only_kept_when_asked; c-store::test_a_trace_never_holds_the_api_key; c-hooks::test_a_recorder_that_raises_does_not_break_the_call; no worker test (the settings reach `llm.reason` through the worker's services) | add to worker: port | S |
| B63 | A saved reply is reused: the backend is not even built, the trace is untouched, the tokens are the recorded ones, the page is byte-identical (backend, model, prompt version as recorded); a plain requeue reuses it | cache::test_a_second_run_reuses_the_saved_reply_and_never_calls_the_backend; cache::test_a_page_from_a_saved_reply_is_byte_identical_to_the_live_page; cache::test_requeue_alone_reuses_the_saved_reply | hllm::test_a_second_run_reuses_the_saved_reply_and_never_calls_the_model; hllm::test_a_saved_reply_is_flagged_and_keeps_the_recorded_tokens; hllm::test_a_saved_reply_is_served_while_its_backend_is_blocked; e2e frozen run; c-hooks::test_a_saved_result_carries_the_recorded_backend_model_and_tokens | covered | S |
| B64 | Fresh calls: `--refresh-llm` calls again and overwrites the trace; a deleted trace gives a fresh call; a new prompt version and another profile are misses; `LLM_CACHE=false` always calls (and still writes) | cache::test_refresh_llm_calls_the_model_again_and_overwrites_the_trace; cache::test_requeue_with_refresh_llm_calls_the_model_again; cache::test_deleting_the_trace_file_gives_a_fresh_call; cache::test_a_prompt_version_change_is_a_miss; cache::test_another_profile_is_a_miss; cache::test_llm_cache_false_always_calls | hllm::test_refresh_llm_in_the_job_params_calls_the_model_again; c-store::test_find_needs_the_same_profile_and_prompt_version; not pinned on the worker: a deleted trace, `LLM_CACHE=false` | add to worker: port | S |
| B65 | A saved reply that is not good is not reused: an `invalid_output` trace, a reply that no longer validates (one warning), a reply that made an invalid page (marked `invalid_page` and committed; a plain requeue asks the model); a new tag order applies to a saved reply | cache::test_an_invalid_output_trace_is_not_reused; cache::test_the_saved_reply_is_validated_again; cache::test_a_reply_that_made_an_invalid_page_is_not_reused; cache::test_a_plain_requeue_after_an_invalid_page_tries_the_model; cache::test_a_new_tag_order_applies_to_a_saved_reply | hllm::test_an_invalid_page_marks_the_trace_invalid_page_and_a_requeue_calls_the_model; c-store::test_find_only_returns_ok_traces; c-hooks::test_saved_replies_that_no_longer_validate_fall_through_to_the_backend; the tag order is not pinned | add to worker: port | S |
| B66 | Which trace gets marked: the file the reply came from (another document's), never a paid trace from a fake run, never with `LLM_TRACE=false`, only the new trace of an edited document | cache::test_an_invalid_page_from_a_reply_saved_for_another_document_marks_that_file; cache::test_a_fake_run_never_marks_a_paid_trace; cache::test_llm_trace_false_never_marks_a_trace; cache::test_an_edited_document_marks_only_its_new_trace | c-store::test_mark_unusable_only_marks_the_trace_that_made_the_page; c-store::test_a_fake_trace_never_replaces_a_real_one; c-store::test_mark_unusable_changes_only_an_ok_trace (shared `reject_invalid_page`); no worker test | add to worker: port | S |
| B67 | A failed refresh or a failed requeue keeps the good paid reply, and the next plain run reuses it | cache::test_a_failed_refresh_keeps_the_good_paid_reply; traces::test_a_failed_requeue_keeps_the_paid_replies_of_the_first_run | c-store::test_an_ok_trace_is_never_replaced_by_a_failed_one; c-store::test_a_failed_retry_does_not_overwrite_a_trace_with_replies | covered | S |
| B68 | `llm_saved` on a report item: false on a first run, true on a reused reply (tokens kept), false with `--refresh-llm` | saved::test_the_flag_is_false_on_a_first_run_true_on_a_reused_reply_and_false_on_refresh | hllm::test_a_saved_reply_is_flagged_and_keeps_the_recorded_tokens (`llm_result.saved`, `tokens_in/out` on the item row). Building the report item from it is B72 | covered | S |

### P. The frozen real run (`tests/data`)

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B69 | The whole pipeline on the committed test data: 43 pages and 1 artifact, no model or YouTube call, traces and facts unchanged, every page equal to the approved one | testdata::test_the_whole_pipeline_on_the_committed_test_data | e2e::test_the_worker_publishes_the_frozen_real_run_with_no_external_call | covered | S |
| B70 | Without `llm/` the same run asks the model (which refuses): no page, the artifact is still copied, no YouTube call | testdata::test_deleting_the_saved_replies_would_call_the_model | GAP (e2e::test_an_llm_failure_makes_no_youtube_call covers one clip only) | add to worker: port | S |
| B71 | The worker's pages equal the old loop's pages on the same data | e2e::test_the_worker_matches_stage_a_on_the_same_data | it is itself the comparison | drop on purpose: it compares the worker with the loop being deleted; B69 compares the worker with the approved pages, which is the stronger check | S |

### Q. The report

| # | behaviour | tests that pin it | worker path today | proposed action | effort |
|---|---|---|---|---|---|
| B72 | The `RunReport` shape: one `ItemReport` per document (status, class, id, page, message, tokens, `llm_saved`), `counts()`, `unreadable`, `not_found`, `not_in_archive`, `problems`, `committed`, `pushed`. Nearly every old test asserts `report.counts()` or `report.items`; the rows above name them | every test above that asserts on the report | only part of it is in the database (see "Report mapping"): `pipeline.run` returns counts, not names; duplicates, artifacts, unreadable files, unmatched names, requeue moves and limit leftovers leave no rows | add to worker: `pipeline.run` returns the names behind its counts (duplicates with their winner, unreadable with the reason, artifacts with status and message, `only`/`requeue` names not found, requeue moves and skips, documents left by the limit, same-id warnings); `report_for_job(session, job_id)` builds the old shape from `job_items`, the job results and the jobs still queued | L |

## Decisions for the user

Every `drop on purpose` row and the one `L` gap, with my recommendation and what a user would lose.

1. **B3, the counts in the commit messages.** Recommendation: drop; the fixed publish messages stay.
   Loss: `git log` no longer says how many pages a commit holds. The `run pipeline` output and `items list`
   still say it.
2. **B5, pull before the work.** Recommendation: drop; publish's order (commit, pull with a rebase, push)
   stays. Loss: with `--push`, a remote you cannot reach no longer stops the run before it starts. The work
   is done and committed locally, the publish job fails, and the next publish pushes it.
   A related point on default 2: Stage A pulled only with `--push`. I recommend `pull = push` for the
   command, not `pull = true`. A repo without an upstream would make every plain run fail
   (hpub::test_a_branch_without_an_upstream_fails_clearly).
3. **B19, the budget message and the per-backend count.** Recommendation: drop the in-run counter and the
   repeated `budget reached (openai)`, and add one cheap line (S): after the drain, the command prints each
   LLM block that is still running, from the `resources` table (backend, until, cause, "raise the key's
   budget or point the profile at another provider"). Loss without that line: no single
   "N note(s) waiting" error. You would read the per-document reasons instead.
   B20 belongs here too: a budget block now lasts 6 h across runs, and no command lifts it early. After
   you top up a key, `--retry-deferred` still defers until the block ends. A `catcher llm unblock
   <backend>` command could come later, outside B5b.
4. **B21, the per-document progress lines `(1/2) ... processing` and `processed 2/2: ...`.**
   Recommendation: drop. The worker's lines are `inbox: N document(s) to process`, one result line per
   document and `pipeline.run: {counts}`. Loss: no position or total during a long run, and the log line
   names the calculated name, not the `inbox/` path.
5. **B71, the worker compared with Stage A.** Recommendation: drop, because the golden comparison (B69)
   stays. Loss: none once the loop is gone.
6. **B72 (L), the report from the database.** Recommendation: extend the `pipeline.run` result with the
   names (one JSON result; no schema change) and build `report_for_job` on it. The alternative is to keep
   only counts for the non-item lines. Loss with the alternative: the per-file lines for duplicates,
   artifacts, unreadable files, `not-found` names and requeue skips. The exit code 1 on a `--file` or
   `--requeue` name that matched nothing would lose its data too. Most of the ~110 ported tests assert
   these lines, so this is the critical path of Task 5.

Also worth a look before Tasks 2 to 6 (changed, not dropped):

- **B56, Ctrl-C.** Should the command still publish what was done after a Ctrl-C (Stage A committed it)?
  I recommend no publish after an interrupt. The command puts its running job back to `queued` and says
  "interrupted: N job(s) left; run `catcher run pipeline` or `catcher worker`", and the next publish
  commits the work. Loss: the finished pages of an interrupted run are not committed until the next run.
- **B57, the lost lock.** The check moves from per document to per job, and the "before the artifacts"
  checkpoint goes away, because `pipeline.run` copies the artifacts in the same job. Loss: a lock lost
  during `pipeline.run` still lets that one job finish staging and copying. Nothing is committed.
- **B45, B46, B56, waiting and interrupted documents stay in `output/`, not `inbox/`.** They are staged with
  an active item and a queued job, and the next run or the worker finishes them. Anyone who looked in
  `inbox/` to see what is left now needs `items list` or the report.

## Report mapping

Old report statuses (`Status` in run.py) and `RunReport` fields, with what the database offers today.
"Run job" means the `pipeline.run` job of this command; "items of the run" means `job_items` with
`root_job_id` = that job.

| old status or field | when (old loop) | what the database offers | gap |
|---|---|---|---|
| `published` | page written, output final | item `status = 'published'`; page = file name of `docs_page`; `tokens_in`, `tokens_out`; `llm_result.saved` (the `llm_saved` flag); `warnings` (`dropped tags: ...`, the old message) | none for staged items. An item put back from its working copy by `retry_deferred` (`_from_working_copy`) keeps its old `root_job_id`: find it through `job_events.job_id` of this run's jobs, or set `root_job_id` there |
| `deferred` | a temporary error; stays in `output/` | item `status = 'deferred'` (or `stuck`: `ItemStates.defer` keeps a stuck item stuck); message = `stage_reason` / `error` | none (`stuck` shows as its own status) |
| `failed` (a document) | invalid page, invalid output, a bug, `could not start work` | item `status = 'failed'`, `error`; the run job's `errors` count for start failures | none |
| `failed` (an artifact) | `copy_artifact` raised | nothing: `copy_artifacts` writes into a throwaway `RunReport` | NEEDS NEW DATA |
| `skipped` (limit) | not worked on because of `--limit` | the handler counts `left_in_inbox` but returns only a log line | NEEDS NEW DATA (a count is enough; the command could also scan `inbox/` again) |
| `skipped` (requeue) | `inbox/<name> already exists, not overwritten` | nothing | NEEDS NEW DATA |
| `skipped` (artifact) | over `ARTIFACT_MAX_MB` | nothing | NEEDS NEW DATA |
| `duplicate` | an earlier snapshot moved to `duplicates/` | run job result `duplicates` (a count); no item row | NEEDS NEW DATA (name and winner) |
| `artifact` | copied to `archive/artifacts` and epiaku-docs | run job result `artifacts` (a count) | NEEDS NEW DATA (names) |
| `requeued` | copied from `archive/` back into `inbox/` | nothing about the move. The requeued item is staged again with this `root_job_id`, and the requested names are in the job's `params.requeue` | NEEDS NEW DATA (which names moved) |
| `waiting` | a clip that must wait for YouTube; stayed in `inbox/` | item `status = 'waiting_youtube'`; its `youtube.fetch` job is `queued` with `run_after` (until when) and `reason` (the gate's message from `Defer`) | none; the status name changes |
| `interrupted` | Ctrl-C during a document | the item keeps its active status; its job stays `running` until reaped (or until the command puts it back, B56) | none if the command reports this run's jobs still `queued`/`running` |
| `would_publish`, `would_copy`, `would_requeue`, `would_fetch` | dry run only | not in the database: the preview (Task 4) builds them | n/a |
| `RunReport.unreadable` | file -> reason, moved to `failed/` | run job result `unreadable` (a count) | NEEDS NEW DATA (names and reasons) |
| `RunReport.not_found` | `--file` names not in `inbox/` | nothing (not even a log line, B31) | NEEDS NEW DATA |
| `RunReport.not_in_archive` | `--requeue` names not in `archive/` | a WARNING log line only | NEEDS NEW DATA |
| `RunReport.problems` | a wrong path, a failed pull/push, an interrupt | `jobs.error` of a failed `pipeline.run` (a wrong path) or `pipeline.publish` (git) | none; the command's own checks (paths, B58) add the rest |
| `RunReport.committed`, `pushed` | the git step at the end | `pipeline.publish` job result `{"committed": {"docs", "ideas"}, "pushed"}` | none |
| message `same id as an earlier document ... replaces the earlier one` | two documents with one id in a run | nothing | NEEDS NEW DATA (B14) |
| new: `adopted`, `errors` | no Stage A equivalent | run job result counts | the report can show them as a line |

The run also drains jobs that earlier runs left queued, such as a clip whose gate has opened. Their items
carry an older `root_job_id`, so a report that reads only `root_job_id` misses them. Task 3 should decide
whether the report shows "also finished: N document(s) from earlier runs" (from the `job_events` of the
jobs this command ran).

## Completeness check

The script below parses the eight files with `ast`. It lists every `test_*` function whose body calls
`run_pipeline(...)` and checks that this document names each one.

```python
import ast, re
from pathlib import Path
files = [
    "tests/unit/test_missing_paths.py", "tests/unit/test_cli.py",
    "tests/integration/db/test_worker_end_to_end.py", "tests/integration/git/test_run.py",
    "tests/integration/git/test_llm_traces_in_run.py", "tests/integration/git/test_llm_cache.py",
    "tests/integration/git/test_testdata_run.py", "tests/integration/git/test_llm_saved_flag.py",
]
doc = Path("docs/superpowers/plans/2026-10-06-idea-catcher-stage-b5b-parity-table.md").read_text()
funcs = [
    f"{Path(f).name}::{n.name}" for f in files for n in ast.parse(Path(f).read_text()).body
    if isinstance(n, ast.FunctionDef) and n.name.startswith("test_") and any(
        isinstance(c, ast.Call) and getattr(c.func, "id", None) == "run_pipeline" for c in ast.walk(n))
]
missing = [f for f in funcs if not re.search(re.escape(f.split("::")[1]) + r"\b", doc)]
```

Result:

| file | call sites | test functions |
|---|---|---|
| tests/unit/test_missing_paths.py | 4 | 4 |
| tests/unit/test_cli.py | 1 | 1 |
| tests/integration/db/test_worker_end_to_end.py | 1 | 1 |
| tests/integration/git/test_run.py | 97 | 71 |
| tests/integration/git/test_llm_traces_in_run.py | 15 | 10 |
| tests/integration/git/test_llm_cache.py | 46 | 21 |
| tests/integration/git/test_testdata_run.py | 2 | 2 |
| tests/integration/git/test_llm_saved_flag.py | 4 | 2 |
| **total** | **170** | **112** |

- 112 test functions call `run_pipeline(`, and all 112 are named in at least one row. `missing` is empty.
- `grep -c "run_pipeline("` counts 98 lines in test_run.py, not 97. The extra line is the `def` of
  `run::test_cli_run_pipeline`, which runs the command and never calls the function. It is in B59 all the
  same, because the command it pins changes in Task 3.
- Three more tests run the `run pipeline` command without calling `run_pipeline(`. They are in B58/B59
  so Task 3 does not miss them: paths::test_the_pipeline_command_with_wrong_paths_exits_2_and_says_which_path,
  and the `rcli` tests, one of which patches `pipeline.run.process_note`
  (rcli::test_a_run_that_loses_its_lock_stops_with_exit_1_before_the_next_document).
- Not a test, but on the same path: `handlers_pipeline.py` imports `ItemReport`, `RunOptions`, `RunReport`,
  `RunState`, `apply_outcome`, `copy_artifacts`, `finish` and `log_outcome` from `pipeline/run.py`. Task 7
  must keep those helpers, or move them, before it deletes the loop.

Row totals: 72 behaviour rows: 30 `covered`, 37 `add to worker`, 5 `drop on purpose` (B3, B5, B19, B21,
B71). Of the 37, these need code as well as a test port: B14, B31, B49, B50, B56, B57, B58, B59, B72 (with
the result data of B35, B39 and B40). Effort: 1 `L` (B72), 6 `M` (B49, B50, B52, B56, B57, B59), 65 `S`.
