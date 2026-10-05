"""`BackendBlocks`: the LLM backends (and profiles) not to call for a while, kept in the `resources` table.

A missing row means OPEN (unlike `youtube`): an LLM block is only ever created by a failure. A Postgres error
while reading counts as BLOCKED for a short while, never as open."""

import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.engine import make_url
from worker_harness import GEMINI_CHAT

from catcher.core.db import make_worker_engine, session_scope
from catcher.modules.llm.backends.fake import FakeBackend
from catcher.modules.llm.service import BudgetExhausted, ModelRejected, UsageLimitReached
from catcher.modules.queue.models import Job, JobItem, Resource
from catcher.modules.queue.queue import claim, enqueue
from catcher.modules.worker.blocks import BackendBlocks
from catcher.modules.worker.handlers import Done
from catcher.modules.worker.handlers_pipeline import handle_llm_reason, handle_pipeline_run
from catcher.modules.youtube.pg_gate import YOUTUBE_RESOURCE

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def url_of(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def row(engine: Engine, name: str) -> Resource | None:
    with session_scope(engine) as session:
        return session.get(Resource, name)


# --- the Postgres memory ---


def test_a_block_survives_a_new_blocks_object_and_a_restart(worker_engine):
    clock = Clock()
    until = NOW + timedelta(seconds=600)
    BackendBlocks(worker_engine, clock=clock).block("openai", until, "openai rate limit: 429")

    restarted = make_worker_engine(url_of(worker_engine))  # a new worker process: a new engine, no memory
    try:
        entries = BackendBlocks(restarted, clock=clock).entries(NOW)
    finally:
        restarted.dispose()
    assert {k: (b.until, b.cause) for k, b in entries.items()} == {
        "openai": (until, "openai rate limit: 429")
    }
    saved = row(worker_engine, "openai")
    assert saved is not None
    assert (saved.blocked_until, saved.blocked_at, saved.reason) == (until, NOW, "openai rate limit: 429")


def test_a_block_expires_with_the_clock(worker_engine):
    blocks = BackendBlocks(worker_engine, clock=Clock())
    assert blocks.active(NOW) == frozenset()  # no row: open
    blocks.block("openai", NOW + timedelta(seconds=600), "budget reached (openai)")
    assert blocks.active(NOW) == frozenset({"openai"})
    assert blocks.active(NOW + timedelta(seconds=599)) == frozenset({"openai"})
    assert blocks.active(NOW + timedelta(seconds=600)) == frozenset()
    assert blocks.entries(NOW + timedelta(seconds=600)) == {}


def test_two_engines_blocking_the_same_backend_keep_one_row_and_the_later_end(worker_engine):
    url = url_of(worker_engine)
    engines = [make_worker_engine(url), make_worker_engine(url)]
    ends = [NOW + timedelta(seconds=600), NOW + timedelta(seconds=21600)]
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            blocks = BackendBlocks(engines[n], clock=Clock())
            barrier.wait(timeout=10)
            blocks.block("openai", ends[n], f"cause {n}")
        except BaseException as error:  # pragma: no cover - only on a bug
            errors.append(error)

    threads = [threading.Thread(target=work, args=(n,), daemon=True) for n in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert all(not thread.is_alive() for thread in threads)
    finally:
        for engine in engines:
            engine.dispose()
    assert errors == []

    with session_scope(worker_engine) as session:
        rows = list(session.scalars(select(Resource).where(Resource.name.like("openai%"))))
    assert [(r.name, r.blocked_until, r.reason) for r in rows] == [("openai", ends[1], "cause 1")]
    blocks = BackendBlocks(worker_engine, clock=Clock())
    assert blocks.active(ends[1] - timedelta(seconds=1)) == frozenset({"openai"})
    assert blocks.active(ends[1]) == frozenset()  # the later end passed: open again


def test_a_shorter_block_does_not_cut_a_longer_one(worker_engine):
    blocks = BackendBlocks(worker_engine, clock=Clock())
    blocks.block("openai", NOW + timedelta(seconds=21600), "openai budget reached")
    blocks.block("openai", NOW + timedelta(seconds=600), "openai rate limit")
    [(key, entry)] = blocks.entries(NOW).items()
    assert (key, entry.until, entry.cause) == (
        "openai",
        NOW + timedelta(seconds=21600),
        "openai budget reached",
    )


def test_a_postgres_error_counts_as_blocked_not_open(worker_engine, caplog):
    broken = make_worker_engine(  # a database that does not exist: every session fails
        make_url(url_of(worker_engine)).set(database="no_such_database").render_as_string(hide_password=False)
    )
    try:
        blocks = BackendBlocks(broken, clock=Clock(), known_backends=lambda: ["openai", "freellmapi"])
        with caplog.at_level(logging.INFO, logger="catcher.worker.blocks"):
            first = blocks.entries(NOW)
            second = blocks.entries(NOW)
            blocks.block("openai", NOW + timedelta(seconds=600), "openai rate limit")  # raises nothing
    finally:
        broken.dispose()
    assert first.keys() == {"openai", "freellmapi"} and second == first
    for entry in first.values():
        assert entry.until == NOW + timedelta(seconds=30)
        assert entry.cause.startswith("block memory unavailable: ")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len([r for r in errors if "read" in r.getMessage()]) == 1  # logged once, not at every read
    assert len([r for r in errors if "openai" in r.getMessage() and "save" in r.getMessage()]) == 1


def test_entries_give_the_time_and_the_cause(worker_engine):
    blocks = BackendBlocks(worker_engine, clock=Clock())
    until = NOW + timedelta(seconds=60)
    blocks.block("freellmapi", until, "connection refused")
    [(backend, entry)] = blocks.entries(NOW).items()
    assert (backend, entry.until, entry.cause) == ("freellmapi", until, "connection refused")


def test_a_new_block_replaces_the_old_one_of_that_backend_only(worker_engine):
    blocks = BackendBlocks(worker_engine, clock=Clock())
    blocks.block("openai", NOW + timedelta(seconds=10), "first")
    blocks.block("freellmapi", NOW + timedelta(seconds=10), "other")
    blocks.block("openai", NOW + timedelta(seconds=100), "again")
    assert blocks.active(NOW + timedelta(seconds=50)) == frozenset({"openai"})
    assert blocks.entries(NOW)["openai"].cause == "again"


def test_rows_of_other_resources_are_not_llm_blocks(worker_engine):
    with session_scope(worker_engine) as session:  # the YouTube gate, closed
        session.execute(
            update(Resource)
            .where(Resource.name == YOUTUBE_RESOURCE)
            .values(blocked_until=NOW + timedelta(hours=6))
        )
    blocks = BackendBlocks(worker_engine, clock=Clock())
    blocks.block("openai:clippings", NOW + timedelta(seconds=60), "model 'gpt-x' rejected")
    assert blocks.active(NOW) == frozenset({"openai:clippings"})


def test_unblock_opens_the_key(worker_engine):
    blocks = BackendBlocks(worker_engine, clock=Clock())
    blocks.block("openai", NOW + timedelta(seconds=60), "x")
    blocks.unblock("openai")
    assert blocks.active(NOW) == frozenset() and row(worker_engine, "openai") is None


def test_the_youtube_gate_row_cannot_be_blocked_or_unblocked_as_an_llm_backend(worker_engine):
    with session_scope(worker_engine) as session:  # the YouTube gate, closed
        session.execute(
            update(Resource)
            .where(Resource.name == YOUTUBE_RESOURCE)
            .values(blocked_until=NOW + timedelta(hours=6), streak=2)
        )
    blocks = BackendBlocks(worker_engine, clock=Clock())
    with pytest.raises(ValueError, match="reserved"):
        blocks.block(YOUTUBE_RESOURCE, NOW + timedelta(seconds=60), "x")
    with pytest.raises(ValueError, match="reserved"):
        blocks.unblock(YOUTUBE_RESOURCE)
    gate = row(worker_engine, YOUTUBE_RESOURCE)
    assert gate is not None
    assert (gate.blocked_until, gate.streak, gate.reason) == (NOW + timedelta(hours=6), 2, None)


@pytest.mark.parametrize("call", ["block", "active", "entries"])
def test_a_naive_time_is_refused(worker_engine, call):
    blocks = BackendBlocks(worker_engine, clock=Clock())
    naive = datetime(2026, 10, 4, 12, 0)
    with pytest.raises(ValueError, match="naive"):
        if call == "block":
            blocks.block("openai", naive, "x")
        elif call == "active":
            blocks.active(naive)
        else:
            blocks.entries(naive)


def test_many_threads_blocking_and_reading_at_once(worker_engine):
    names = [f"b{n}" for n in range(8)]
    blocks = BackendBlocks(
        worker_engine, clock=Clock(), known_backends=lambda: [*names, *(f"gone{n}" for n in range(8))]
    )
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(20):
                blocks.block(f"b{n}", NOW + timedelta(seconds=1 + i % 3), "x")
                blocks.active(NOW)
                blocks.entries(NOW)
                blocks.block(f"gone{n}", NOW, "ends at once")  # not active at NOW
        except BaseException as e:  # pragma: no cover - only on a bug
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,), daemon=True) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(not t.is_alive() for t in threads)
    assert errors == []
    assert blocks.active(NOW) == frozenset(names)


def test_llm_resource_rows_never_hold_back_other_jobs(worker_engine):
    blocks = BackendBlocks(worker_engine, clock=Clock())
    for key in ("openai", "freellmapi", "fake", "openai:clippings"):
        blocks.block(key, NOW + timedelta(hours=6), "closed")
    with session_scope(worker_engine) as session:
        expected = {
            enqueue(session, type="llm.reason", now=NOW, params={"calculated_name": "a.md"})[0].id,
            enqueue(session, type="pipeline.run", now=NOW)[0].id,
            enqueue(session, type="youtube.fetch", now=NOW, resource=YOUTUBE_RESOURCE)[
                0
            ].id,  # its gate: open
        }
    claimed = set()
    with session_scope(worker_engine) as session:
        while (job := claim(session, worker="w1", now=NOW, lease_s=30)) is not None:
            claimed.add(job.id)
    assert claimed == expected


# --- through the `llm.reason` handler ---


def stage(harness, **params) -> None:
    job_id = harness.add_job("pipeline.run", **params)
    with session_scope(harness.ctx.engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert isinstance(handle_pipeline_run(harness.ctx, job), Done)
        session.execute(update(Job).where(Job.id == job_id).values(status="succeeded"))


def item_of(harness, original: str) -> JobItem:
    with session_scope(harness.ctx.engine) as session:
        [item] = [i for i in session.scalars(select(JobItem)) if i.original_filename == original]
        return item


def reason(harness, original: str):
    name = item_of(harness, original).calculated_name
    with session_scope(harness.ctx.engine) as session:
        [job] = [
            j
            for j in harness.jobs(session)
            if j.type == "llm.reason" and j.params["calculated_name"] == name and j.status == "queued"
        ]
    result = handle_llm_reason(harness.ctx, job)
    with session_scope(harness.ctx.engine) as session:
        session.execute(update(Job).where(Job.id == job.id).values(status="succeeded"))
    return result


def another_chat(harness, filename: str, chat_id: str) -> None:
    other = GEMINI_CHAT.replace("cf81e40b020519ef", chat_id)  # another chat, same class (clippings profile)
    (harness.ideas / "inbox/clippings" / filename).write_text(other, encoding="utf-8")


def test_a_budget_block_is_six_hours_and_a_usage_limit_ten_minutes(harness):
    stage(harness, only=["YouTube walks", "systeme"])  # a note (fake backend) and a chat (openai)
    harness.backends.note = FakeBackend([BudgetExhausted("fake budget reached", backend="fake")])
    harness.backends.chat = FakeBackend([UsageLimitReached("openai rate limit: 429", backend="openai")])

    assert reason(harness, "YouTube walks.md") == Done({"item": "deferred"})
    assert reason(harness, "systeme.md") == Done({"item": "deferred"})

    entries = harness.ctx.backend_blocks.entries(harness.clock())
    assert entries["fake"].until == harness.clock() + timedelta(hours=6)
    assert entries["openai"].until == harness.clock() + timedelta(minutes=10)
    assert (harness.ctx.settings.llm_budget_block_s, harness.ctx.settings.llm_block_s) == (21600, 600)


def test_a_wrong_model_blocks_the_profile_not_the_backend(harness):
    stage(harness, only=["systeme"])  # the Gemini chat: clippings profile, openai
    harness.backends.chat = FakeBackend(
        [ModelRejected("openai: model 'gpt-tset' rejected (HTTP 404)", backend="openai", model="gpt-tset")]
    )
    assert reason(harness, "systeme.md") == Done({"item": "deferred"})

    entries = harness.ctx.backend_blocks.entries(harness.clock())
    assert set(entries) == {"openai:clippings"}
    assert entries["openai:clippings"].until == harness.clock() + timedelta(seconds=600)
    assert "gpt-tset" in entries["openai:clippings"].cause
    assert "gpt-tset" in (item_of(harness, "systeme.md").error or "")  # the reason names the model

    another_chat(harness, "again.md", "925d9b0b4ca21b63")  # the same profile: not called again
    stage(harness, only=["again"])
    harness.backends.chat = FakeBackend()
    assert reason(harness, "again.md") == Done({"item": "deferred"})
    assert harness.backends.chat.prompts == []
    assert "not called again until" in (item_of(harness, "again.md").error or "")


def test_other_profiles_of_the_same_backend_still_run(harness):
    stage(harness, only=["systeme"])
    harness.backends.chat = FakeBackend(
        [ModelRejected("openai: model 'gpt-tset' rejected (HTTP 404)", backend="openai", model="gpt-tset")]
    )
    assert reason(harness, "systeme.md") == Done({"item": "deferred"})

    another_chat(harness, "other.md", "925d9b0b4ca21b63")
    stage(harness, only=["other"], profile="youtube")  # the youtube profile: same openai backend
    assert reason(harness, "other.md") == Done({"item": "published"})
    assert len(harness.backends.chat.prompts) == 2  # the rejected call, then this one
    assert harness.ctx.backend_blocks.active(harness.clock()) == frozenset({"openai:clippings"})
