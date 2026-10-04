"""The worker end to end on the frozen real run (`tests/data`): Stage A's pages, and no external call.

The committed test data carries the saved YouTube facts and LLM replies, so the worker processes the whole
inbox with a model and a YouTube that only record an attempt and raise (`frozen_harness`). The frozen repos
have no git remote, so `pipeline.publish` runs with `push=false, pull=false`: it commits, nothing more.

Review Focus #3: an LLM failure never causes a YouTube call. The facts are fetched once, by `youtube.fetch`,
and a deferred item that is retried later reads the saved facts."""

from datetime import timedelta
from pathlib import Path

from golden import EXPECTED, PAGES, compare_pages
from sqlalchemy import select
from worker_harness import ExternalCall, FrozenHarness, RaisingBackend

from catcher.core.db import session_scope
from catcher.core.testdata import reset_test_repos
from catcher.modules.llm.backends.fake import FakeBackend
from catcher.modules.pipeline.run import RunOptions, run_pipeline
from catcher.modules.queue.models import Job, JobItem
from catcher.modules.youtube.access import YoutubeAccess
from catcher.modules.youtube.facts import YoutubeFacts
from catcher.modules.youtube.gate import YoutubeGate

MAX_JOBS = 200  # 44 documents: a pipeline.run, then an llm.reason per page (and a fetch per clip at most)
SIX_PROVEN = "youtube source - 6 Proven Strategies That Turn Viewers In To Buyers.md"
RAG = "youtube source - RAG + Langchain Python.md"
SIX_PROVEN_VID, RAG_VID = "RlX-tt9XrFo", "tcqEUSNCn8I"


def file_bytes(folder: Path) -> dict[Path, bytes]:
    return {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def items(h: FrozenHarness) -> dict[str, JobItem]:
    """Every item row, by the name it was captured under."""
    with session_scope(h.ctx.engine) as session:
        return {i.original_filename or i.calculated_name: i for i in session.scalars(select(JobItem))}


def jobs_of(h: FrozenHarness, type: str) -> list[Job]:
    with session_scope(h.ctx.engine) as session:
        return [j for j in h.jobs(session) if j.type == type]


def run_and_publish(h: FrozenHarness) -> list[str]:
    """`pipeline.run`, drain, then `pipeline.publish` (commit only: no remote), drain; the job outcomes."""
    h.add_job("pipeline.run")
    labels = h.drain(max_jobs=MAX_JOBS)
    h.add_job("pipeline.publish", push=False, pull=False)
    return labels + h.drain(max_jobs=2)


def forget_saved(h: FrozenHarness, sh, *paths: Path) -> None:
    """Delete saved facts or replies from the reset copy (never from tests/data) and commit that."""
    for path in paths:
        path.unlink()
    sh(h.ideas, "commit", "-qam", "forget some saved facts and replies")


def frozen_facts(h: FrozenHarness, *vids: str) -> dict[str, YoutubeFacts]:
    """The saved facts of `vids`, read from the copy before a test deletes them."""
    return {
        vid: YoutubeFacts.model_validate_json((h.ideas / "facts" / f"{vid}.json").read_text(encoding="utf-8"))
        for vid in vids
    }


def test_the_worker_publishes_the_frozen_real_run_with_no_external_call(frozen_harness, sh):
    h = frozen_harness
    ideas, docs = h.ideas, h.docs
    traces_before, facts_before = file_bytes(ideas / "llm"), file_bytes(ideas / "facts")

    labels = run_and_publish(h)

    assert labels and set(labels) == {"succeeded"}, labels
    assert compare_pages(docs / PAGES, EXPECTED) == 43
    artifacts = [p.name for p in (docs / "idea-bucket/artifacts").iterdir() if p.name != ".gitkeep"]
    assert len(artifacts) == 1 and artifacts[0].endswith("-sample-report.pdf")
    assert h.model_calls == [] and h.fetch_calls == []
    assert file_bytes(ideas / "llm") == traces_before  # read, never rewritten, and no new trace
    assert file_bytes(ideas / "facts") == facts_before
    assert not [p for p in (ideas / "inbox").rglob("*") if p.is_file()]  # everything was worked on
    assert sh(ideas, "status", "--porcelain") == "" and sh(docs, "status", "--porcelain") == ""
    rows = items(h)
    assert len(rows) == 43 and {i.status for i in rows.values()} == {"published"}


def test_the_worker_matches_stage_a_on_the_same_data(frozen_harness, make_services, tmp_path):
    h = frozen_harness
    stage_a = reset_test_repos(tmp_path / "stage-a")
    model_calls: list[str] = []
    youtube_calls: list[str] = []

    def no_youtube(vid: str) -> YoutubeFacts:
        youtube_calls.append(vid)
        raise ExternalCall(f"tests must not call YouTube ({vid})")

    services = make_services(facts=no_youtube)
    services.backends = lambda profile: RaisingBackend(model_calls)
    gate = YoutubeGate(tmp_path / "stage-a-gate", min_gap_s=600, jitter_s=0, block_hours=6)
    services.youtube = YoutubeAccess(no_youtube, gate, wait_max_s=0)
    report = run_pipeline(stage_a["idea-bucket"], stage_a["epiaku-docs"], RunOptions(), services)
    assert report.counts() == {"published": 43, "artifact": 1}
    assert model_calls == [] and youtube_calls == []

    assert set(run_and_publish(h)) == {"succeeded"}

    assert compare_pages(h.docs / PAGES, stage_a["epiaku-docs"] / PAGES) == 43
    assert h.model_calls == [] and h.fetch_calls == []


def test_an_llm_failure_makes_no_youtube_call(frozen_harness, sh):
    """One direct clip without its saved facts and reply, and a model that is down: YouTube is asked once."""
    h = frozen_harness
    facts = frozen_facts(h, SIX_PROVEN_VID)
    [reply] = (h.ideas / "llm" / "clippings").glob("*-youtube-source-6-proven-strategies-*.json")
    forget_saved(h, sh, h.ideas / "facts" / f"{SIX_PROVEN_VID}.json", reply)
    h.fetcher = lambda vid: facts[vid]  # the frozen harness's chat backend raises BackendUnavailable

    h.add_job("pipeline.run")
    assert set(h.drain(max_jobs=MAX_JOBS)) == {"succeeded"}

    assert h.fetch_calls == [SIX_PROVEN_VID]  # fetched once, by youtube.fetch
    assert h.model_calls  # the model was asked for that clip, and refused
    rows = items(h)
    assert rows[SIX_PROVEN].status == "deferred"
    assert {i.status for n, i in rows.items() if n != SIX_PROVEN} == {"published"}
    assert (h.ideas / "facts" / f"{SIX_PROVEN_VID}.json").is_file()  # the fetch saved the facts

    h.backends.chat = FakeBackend()  # the model is back
    h.clock.advance(h.ctx.settings.llm_block_s + 1)  # and the worker's block of it has ended (LLM_BLOCK_S)
    h.add_job("pipeline.run", retry_deferred=True)
    assert set(h.drain(max_jobs=MAX_JOBS)) == {"succeeded"}

    assert items(h)[SIX_PROVEN].status == "published"
    assert h.fetch_calls == [SIX_PROVEN_VID]  # still one: the retry read the saved facts
    assert len(h.backends.chat.prompts) == 1
    assert list((h.docs / PAGES / "youtube").glob("*-youtube-source-6-proven-strategies-*.md"))


def test_a_closed_gate_waits_in_the_queue_and_the_next_slot_publishes(frozen_harness, sh):
    """Both direct clips without saved facts: the gate allows one fetch, the other job waits for the slot."""
    h = frozen_harness
    facts = frozen_facts(h, SIX_PROVEN_VID, RAG_VID)
    forget_saved(h, sh, *(h.ideas / "facts" / f"{vid}.json" for vid in facts))
    h.fetcher = lambda vid: facts[vid]
    slot = h.clock() + timedelta(seconds=600)

    h.add_job("pipeline.run")
    labels = h.drain(max_jobs=MAX_JOBS)

    assert labels.count("deferred") == 1 and set(labels) == {"succeeded", "deferred"}
    [first] = h.fetch_calls  # the gate allowed one call
    second, second_clip = (RAG_VID, RAG) if first == SIX_PROVEN_VID else (SIX_PROVEN_VID, SIX_PROVEN)
    [waiting] = [j for j in jobs_of(h, "youtube.fetch") if j.status == "queued"]
    assert (waiting.run_after, waiting.attempts) == (slot, 0)  # deferred to the slot, no attempt counted
    assert items(h)[second_clip].status == "waiting_youtube"
    assert len(list((h.docs / PAGES).rglob("2*.md"))) == 42  # every page but the waiting clip's

    h.clock.advance(600)  # the slot has come
    assert set(h.drain(max_jobs=MAX_JOBS)) == {"succeeded"}

    assert h.fetch_calls == [first, second]
    assert {i.status for i in items(h).values()} == {"published"}
    assert h.model_calls == []  # the fetched facts equal the saved ones: the saved replies still match
    assert compare_pages(h.docs / PAGES, EXPECTED) == 43
