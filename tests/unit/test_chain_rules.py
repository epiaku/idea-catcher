"""The chain's "what next?" rule: a pure function of the job that ended and a few facts."""

from dataclasses import replace

import pytest

from catcher.modules.worker.chain import Facts, Next, decide, end_of_chain

IDLE = Facts(
    inbox_empty=True,
    deferred_due=False,
    unpublished_work=False,
    document_jobs_runnable=0,
    run_active=False,
    latest_run_limited=False,
    last_run_made_progress=True,
    chain_publish_push=True,
    backfill_released=False,
)

RUN_AFTER_PULL = Next("pipeline.run", {"retry_deferred": True}, "chain:run")
FOLLOW_UP_RUN = Next("pipeline.run", {"retry_deferred": False}, "chain:run")


def publish(*, push: bool = True, follow_up: bool = True) -> Next:
    return Next("pipeline.publish", {"pull": push, "push": push, "follow_up": follow_up}, "chain:publish")


# ideas.pull


def test_a_pull_with_new_files_queues_a_run_that_retries_the_deferred() -> None:
    assert decide("ideas.pull", "succeeded", {}, replace(IDLE, inbox_empty=False)) == RUN_AFTER_PULL


def test_a_pull_with_a_deferred_item_due_queues_a_run() -> None:
    assert decide("ideas.pull", "succeeded", {}, replace(IDLE, deferred_due=True)) == RUN_AFTER_PULL


def test_a_pull_with_nothing_to_run_but_unpublished_work_queues_a_publish() -> None:
    assert decide("ideas.pull", "succeeded", {}, replace(IDLE, unpublished_work=True)) == publish()


def test_a_pull_with_nothing_to_do_queues_nothing() -> None:
    assert decide("ideas.pull", "succeeded", {}, IDLE) is None


def test_the_limit_of_a_pull_goes_on_to_its_run() -> None:
    facts = replace(IDLE, inbox_empty=False)
    assert decide("ideas.pull", "succeeded", {"limit": 3}, facts) == Next(
        "pipeline.run", {"retry_deferred": True, "limit": 3}, "chain:run"
    )


def test_a_pull_without_a_limit_gives_its_run_no_limit() -> None:
    chosen = decide("ideas.pull", "succeeded", {}, replace(IDLE, inbox_empty=False))
    assert chosen is not None and "limit" not in chosen.params


@pytest.mark.parametrize("outcome", ["failed", "deferred", "lost"])
def test_a_pull_that_did_not_succeed_queues_nothing(outcome: str) -> None:
    assert decide("ideas.pull", outcome, {}, replace(IDLE, inbox_empty=False)) is None


def test_the_publish_of_a_pull_follows_the_push_setting() -> None:
    facts = replace(IDLE, unpublished_work=True, chain_publish_push=False)
    assert decide("ideas.pull", "succeeded", {}, facts) == publish(push=False)


# the run and the document jobs


@pytest.mark.parametrize("job_type", ["pipeline.run", "youtube.fetch", "llm.reason"])
@pytest.mark.parametrize("outcome", ["succeeded", "deferred"])
def test_the_last_job_that_can_run_now_queues_the_publish(job_type: str, outcome: str) -> None:
    assert decide(job_type, outcome, {}, IDLE) == publish()


@pytest.mark.parametrize("job_type", ["youtube.fetch", "llm.reason"])
def test_a_failed_document_job_still_counts_as_ended(job_type: str) -> None:
    assert decide(job_type, "failed", {}, IDLE) == publish()


def test_a_failed_run_stops_the_chain() -> None:
    # it could not even read the inbox: a publish and a new run would only fail the same way, for ever
    assert decide("pipeline.run", "failed", {}, replace(IDLE, inbox_empty=False)) is None


@pytest.mark.parametrize("job_type", ["pipeline.run", "youtube.fetch", "llm.reason"])
def test_nothing_is_queued_while_another_document_job_can_run_now(job_type: str) -> None:
    assert decide(job_type, "succeeded", {}, replace(IDLE, document_jobs_runnable=1)) is None


def test_a_document_job_that_is_only_delayed_does_not_hold_the_publish_back() -> None:
    # a delayed job (the YouTube gate, a retry delay) is not "runnable now": the facts do not count it
    assert decide("llm.reason", "succeeded", {}, replace(IDLE, document_jobs_runnable=0)) == publish()


@pytest.mark.parametrize("job_type", ["youtube.fetch", "llm.reason"])
def test_nothing_is_queued_while_a_run_is_active(job_type: str) -> None:
    assert decide(job_type, "succeeded", {}, replace(IDLE, run_active=True)) is None


def test_the_publish_after_a_limited_run_does_not_follow_up() -> None:
    assert decide("llm.reason", "succeeded", {}, replace(IDLE, latest_run_limited=True)) == publish(
        follow_up=False
    )


def test_the_publish_follows_the_push_setting() -> None:
    assert decide("llm.reason", "succeeded", {}, replace(IDLE, chain_publish_push=False)) == publish(
        push=False
    )


@pytest.mark.parametrize("job_type", ["pipeline.run", "youtube.fetch", "llm.reason", "pipeline.publish"])
def test_a_lost_job_queues_nothing(job_type: str) -> None:
    assert decide(job_type, "lost", {}, IDLE) is None


# the publish


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_a_publish_with_files_left_in_the_inbox_queues_a_run_without_retry(outcome: str) -> None:
    assert decide("pipeline.publish", outcome, {}, replace(IDLE, inbox_empty=False)) == FOLLOW_UP_RUN


def test_a_publish_with_an_empty_inbox_ends_the_cycle() -> None:
    assert decide("pipeline.publish", "succeeded", {}, IDLE) is None


def test_a_publish_with_follow_up_false_ends_the_cycle_even_with_files_left() -> None:
    facts = replace(IDLE, inbox_empty=False)
    assert decide("pipeline.publish", "succeeded", {"follow_up": False}, facts) is None


def test_a_publish_does_not_queue_a_run_when_the_last_run_made_no_progress() -> None:
    # files the scan keeps leaving behind would otherwise loop for ever
    facts = replace(IDLE, inbox_empty=False, last_run_made_progress=False)
    assert decide("pipeline.publish", "succeeded", {}, facts) is None


def test_a_publish_does_not_queue_a_run_while_one_is_active() -> None:
    assert (
        decide("pipeline.publish", "succeeded", {}, replace(IDLE, inbox_empty=False, run_active=True)) is None
    )


@pytest.mark.parametrize("outcome", ["deferred", "lost"])
def test_a_publish_that_was_put_back_or_lost_queues_nothing(outcome: str) -> None:
    assert decide("pipeline.publish", outcome, {}, replace(IDLE, inbox_empty=False)) is None


# backfill jobs (backfill.docs, backfill.channel, backfill.release): they release notes into the inbox, a run
# takes them


RELEASED = replace(IDLE, inbox_empty=False, backfill_released=True)


@pytest.mark.parametrize("job_type", ["backfill.docs", "backfill.channel", "backfill.release"])
def test_backfill_that_released_notes_with_files_in_the_inbox_queues_a_run(job_type: str) -> None:
    assert decide(job_type, "succeeded", {}, RELEASED) == FOLLOW_UP_RUN


@pytest.mark.parametrize("job_type", ["backfill.docs", "backfill.channel", "backfill.release"])
def test_backfill_that_released_nothing_queues_nothing_even_with_files_waiting(job_type: str) -> None:
    assert decide(job_type, "succeeded", {}, replace(RELEASED, backfill_released=False)) is None


@pytest.mark.parametrize("job_type", ["backfill.docs", "backfill.channel", "backfill.release"])
def test_a_dry_run_queues_nothing_whatever_the_facts(job_type: str) -> None:
    assert decide(job_type, "succeeded", {"dry_run": True}, RELEASED) is None


@pytest.mark.parametrize("job_type", ["backfill.docs", "backfill.channel", "backfill.release"])
def test_backfill_succeeded_with_empty_inbox_queues_nothing(job_type: str) -> None:
    assert decide(job_type, "succeeded", {}, replace(RELEASED, inbox_empty=True)) is None


@pytest.mark.parametrize("job_type", ["backfill.docs", "backfill.channel", "backfill.release"])
def test_backfill_succeeded_while_a_run_is_active_queues_nothing(job_type: str) -> None:
    assert decide(job_type, "succeeded", {}, replace(RELEASED, run_active=True)) is None


@pytest.mark.parametrize("outcome", ["failed", "deferred", "lost"])
@pytest.mark.parametrize("job_type", ["backfill.docs", "backfill.channel", "backfill.release"])
def test_backfill_failed_and_deferred_queue_nothing(job_type: str, outcome: str) -> None:
    assert decide(job_type, outcome, {}, RELEASED) is None


def test_a_backfill_job_needs_only_the_inbox_run_and_release_facts() -> None:
    from catcher.modules.worker.chain import _needed

    assert _needed("backfill.docs") == {"inbox_empty", "run_active", "backfill_released"}
    assert _needed("backfill.channel") == {"inbox_empty", "run_active", "backfill_released"}
    assert _needed("backfill.release") == {"inbox_empty", "run_active", "backfill_released"}


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "deferred"])
def test_a_backfill_job_closes_no_cycle_in_the_log(outcome: str) -> None:
    assert end_of_chain("backfill.docs", outcome, {}, IDLE) is None


# other job types


@pytest.mark.parametrize("job_type", ["pipeline.preview", "test.echo"])
def test_other_job_types_queue_nothing(job_type: str) -> None:
    assert decide(job_type, "succeeded", {}, replace(IDLE, inbox_empty=False)) is None


# the params of ideas.pull


def test_a_pull_accepts_only_a_whole_number_limit() -> None:
    from catcher.modules.worker.app import check_job

    check_job("ideas.pull", {})
    check_job("ideas.pull", {"limit": 0})
    check_job("ideas.pull", {"limit": 3})
    for bad in (-1, "3", 1.5, True, None):
        with pytest.raises(ValueError, match="limit"):
            check_job("ideas.pull", {"limit": bad})
    with pytest.raises(ValueError, match="only"):
        check_job("ideas.pull", {"only": ["a.md"]})


# the line that closes a cycle in the log


def test_a_publish_that_ends_the_cycle_says_so_prominently() -> None:
    line = end_of_chain("pipeline.publish", "succeeded", {}, IDLE)
    assert line is not None and line.startswith("🏆🏆🏆 chain complete")
    assert "inbox is empty" in line


def test_a_limited_cycle_says_the_rest_waits() -> None:
    line = end_of_chain(
        "pipeline.publish", "succeeded", {"follow_up": False}, replace(IDLE, inbox_empty=False)
    )
    assert line is not None and line.startswith("🏆🏆🏆 chain complete") and "limit" in line


def test_a_failed_publish_that_ends_the_cycle_is_a_warning_not_a_celebration() -> None:
    line = end_of_chain("pipeline.publish", "failed", {}, IDLE)
    assert line is not None and line.startswith("⚠️ chain ended") and "publish failed" in line


def test_a_cycle_stopped_by_a_failure_says_so() -> None:
    for job_type in ("ideas.pull", "pipeline.run"):
        line = end_of_chain(job_type, "failed", {}, IDLE)
        assert line is not None and line.startswith("🛑 chain stopped") and job_type in line


def test_a_pull_with_nothing_to_do_is_idle() -> None:
    line = end_of_chain("ideas.pull", "succeeded", {}, IDLE)
    assert line is not None and line.startswith("💤 chain idle")


def test_a_publish_without_progress_says_why_no_run_follows() -> None:
    facts = replace(IDLE, inbox_empty=False, last_run_made_progress=False)
    line = end_of_chain("pipeline.publish", "succeeded", {}, facts)
    assert line is not None and line.startswith("🛑 chain stopped") and "took none" in line


def test_nothing_is_said_while_the_cycle_goes_on() -> None:
    assert end_of_chain("llm.reason", "succeeded", {}, replace(IDLE, document_jobs_runnable=1)) is None
    assert end_of_chain("pipeline.run", "succeeded", {}, replace(IDLE, run_active=True)) is None
    assert (
        end_of_chain("pipeline.publish", "succeeded", {}, replace(IDLE, run_active=True, inbox_empty=False))
        is None
    )
    assert end_of_chain("pipeline.publish", "deferred", {}, IDLE) is None
    assert end_of_chain("llm.reason", "lost", {}, IDLE) is None
    assert end_of_chain("pipeline.preview", "succeeded", {}, IDLE) is None


def test_a_job_that_queues_something_ends_nothing() -> None:
    facts = replace(IDLE, inbox_empty=False)
    assert decide("ideas.pull", "succeeded", {}, facts) is not None
    assert end_of_chain("ideas.pull", "succeeded", {}, facts) is None
