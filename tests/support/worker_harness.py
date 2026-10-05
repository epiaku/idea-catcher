"""A worker on real Git repos and a real Postgres queue, with a fake model, a fake YouTube and a frozen clock.

The fixtures `harness` and `frozen_harness` in `tests/integration/db/conftest.py` build these:

- `WorkerHarness`: the seed of `tests/integration/git/test_run.py` (a note, a Gemini chat, a YouTube clip)
  in a bare + clone idea-bucket and epiaku-docs; fake note/chat backends (`.backends.note` / `.backends.chat`,
  swap them freely); a counting fake YouTube fetcher (`.fetch_calls`, `.fetcher` to change what it does)
  behind a real `YoutubeAccess` with the worker's Postgres gate (the row `youtube`, re-seeded open for each
  test by the db conftest) on the harness's engine, reading the frozen clock.
- `frozen_harness()`: the same machinery on `reset_test_repos` of the committed `tests/data` (the real inbox,
  saved facts and saved replies), with a model and a YouTube that record the attempt and raise.

Nothing here touches the network, the developer's database or `tests/data` (reset copies it).

Two things to know when writing tests on these:

- The frozen repos have **no git remote** (`reset_test_repos` makes them so): a handler that pulls or pushes
  needs the test to handle that (give the repos a bare remote, or expect the failure). The seeded harness has
  bare + clone repos, so pull and push work there.
- The raising fakes are swallowed like any other error: the pipeline treats the model's refusal as an outage
  (the item is deferred), and a handler or `run_job` catches the YouTube `ExternalCall` with
  `except Exception` (the job fails). So a frozen test proves "no external call" with `model_calls == []`
  and `fetch_calls == []`, never by waiting for the exception to reach it.
"""

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from catcher.core.config import Settings
from catcher.core.db import session_scope
from catcher.core.testdata import reset_test_repos
from catcher.modules.llm.backends.fake import FakeBackend
from catcher.modules.llm.profiles import Profile
from catcher.modules.llm.service import BackendReply, TransientBackendError
from catcher.modules.pipeline.process import Services
from catcher.modules.queue.models import Job
from catcher.modules.queue.queue import enqueue
from catcher.modules.queue.states import ItemStates
from catcher.modules.worker.app import build_handlers
from catcher.modules.worker.blocks import BackendBlocks
from catcher.modules.worker.handlers import Handler, HandlerContext, frontmatter_mirror
from catcher.modules.worker.loop import Worker
from catcher.modules.youtube.access import YoutubeAccess
from catcher.modules.youtube.facts import YoutubeFacts
from catcher.modules.youtube.pg_gate import PostgresGate

NOTES = "hugo/content/en/docs/idea-bucket/notes"
WEB_CLIPS = "hugo/content/en/docs/idea-bucket/web-clips"
GEMINI_CHAT = (
    '---\nsource : "https://gemini.google.com/app/cf81e40b020519ef?is_sa=1"\n'
    'created: 2026-09-25\ntags:\n  - "clippings"\n---\n'
    + "**You**\n\nsell bundles?\n\n---\n\n**Gemini**\n\nYes.\n"
)
YT_CLIP = (
    '---\nsource : "https://www.youtube.com/watch?v=nGVZS_wUDGM&list=PL1&t=1s"\n'
    "created: 2026-09-25\n---\nclip\n"
)
IDEAS_SEED = {
    "inbox/notes/YouTube walks.md": "Create YouTube content walking around\n",
    "inbox/clippings/systeme.md": GEMINI_CHAT,
    "inbox/clippings/yt.md": YT_CLIP,
}
DOCS_SEED = {
    f"{NOTES}/_index.md": "---\ntitle: Notes\n---\n",
    f"{WEB_CLIPS}/_index.md": "---\ntitle: Web clips\n---\n",
}
YOUTUBE_GAP_S = 600  # the gate allows one fetch, then the next slot is 10 minutes later (no jitter)


class FrozenClock:
    """An aware UTC clock that only moves when told. The YouTube gate reads the same time."""

    def __init__(self, now: datetime = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def timestamp(self) -> float:
        return self.now.timestamp()

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class ExternalCall(AssertionError):
    """A test called a model or YouTube that it must not call."""


class RaisingBackend:
    """A model backend that records the task it was asked for, then refuses (a transient outage)."""

    name = "raising"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def complete(self, prompt: str, *, model: str | None, task: str) -> BackendReply:
        self.calls.append(task)
        raise TransientBackendError(f"tests must not call a model ({task})")


class WorkerHarness:
    """A `Worker` with no idle sleep (it never waits), on the two repos, the queue at `engine`, and fakes.

    `.backends.note` serves the fake/notes profiles and `.backends.chat` the openai ones (clippings, YouTube);
    `.fetcher(video_id)` is what the fake YouTube does after it recorded the call in `.fetch_calls`."""

    def __init__(
        self,
        *,
        ideas: Path,
        docs: Path,
        engine: Engine,
        services: Services,
        fetcher: Callable[[str], YoutubeFacts],
        backends: SimpleNamespace,
        handlers: Mapping[str, Handler] | None = None,
        clock: FrozenClock | None = None,
    ) -> None:
        self.ideas, self.docs = ideas, docs
        self.clock = clock or FrozenClock()
        self.fetch_calls: list[str] = []
        self.fetcher = fetcher
        self.backends = backends
        self.gate = PostgresGate(  # the worker's gate: the row `youtube` on the harness's engine
            engine, min_gap_s=YOUTUBE_GAP_S, jitter_s=0, block_hours=6, clock=self.clock.timestamp
        )
        services.settings = Settings(
            ideas_repo=ideas,
            docs_repo=docs,
            database_url=engine.url.render_as_string(hide_password=False),
            youtube_offline=False,
        )
        services.backends = self._backend_for
        services.youtube = YoutubeAccess(
            self._fetch, self.gate, clock=self.clock.timestamp, sleep=self.clock.advance, wait_max_s=0
        )
        self.ctx = HandlerContext(
            settings=services.settings,
            services=services,
            engine=engine,
            ideas=ideas,
            docs=docs,
            clock=self.clock,
            backend_blocks=BackendBlocks(
                engine, clock=self.clock
            ),  # the LLM blocks in Postgres, as the worker
            item_states=ItemStates(mirror=frontmatter_mirror(ideas)),  # as build_context makes it
        )
        self.worker = Worker(
            self.ctx, dict(handlers if handlers is not None else build_handlers()), worker_id="harness"
        )

    def _backend_for(self, profile: Profile) -> Any:
        return self.backends.chat if profile.backend == "openai" else self.backends.note

    def _fetch(self, video_id: str) -> YoutubeFacts:
        self.fetch_calls.append(video_id)
        return self.fetcher(video_id)

    def drain(self, max_jobs: int = 50) -> list[str]:
        """Run due jobs until none is left; the label of each is its `run_once` outcome. Raises AssertionError
        when `max_jobs` jobs ran and the worker still did not go idle (a job loop, or a deferral that stays
        due under the frozen clock). A test that truly runs N jobs passes `max_jobs=N + 1`."""
        labels: list[str] = []
        while len(labels) < max_jobs:
            outcome = self.worker.run_once()
            if outcome is None:
                return labels
            labels.append(outcome)
        raise AssertionError(f"worker did not go idle after {max_jobs} jobs: {labels}")

    def add_job(self, type: str, *, priority: int = 0, **params: Any) -> uuid.UUID:
        """Queue a job now (committed) and return its id."""
        with session_scope(self.ctx.engine) as session:
            job, _ = enqueue(session, type=type, now=self.clock(), priority=priority, params=params)
            return job.id

    @staticmethod
    def jobs(session: Session) -> list[Job]:
        """Every job, oldest first."""
        return list(session.scalars(select(Job).order_by(Job.created_at, Job.id)))


def seeded_harness(
    *,
    make_repo: Callable[[str, dict[str, str]], tuple[Path, Path]],
    engine: Engine,
    services: Services,
    yt_facts: YoutubeFacts,
    handlers: Mapping[str, Handler] | None = None,
) -> WorkerHarness:
    """The harness on the test_run seed; the fake YouTube returns `yt_facts` under the asked video id."""
    _, ideas = make_repo("idea-bucket", IDEAS_SEED)
    _, docs = make_repo("epiaku-docs", DOCS_SEED)

    def facts_for(video_id: str) -> YoutubeFacts:
        return yt_facts.model_copy(
            update={"video_id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}"}
        )

    return WorkerHarness(
        ideas=ideas,
        docs=docs,
        engine=engine,
        services=services,
        fetcher=facts_for,
        backends=SimpleNamespace(note=FakeBackend(), chat=FakeBackend()),
        handlers=handlers,
    )


class FrozenHarness(WorkerHarness):
    """The harness on the committed test data; `.model_calls` records every attempt to call a model."""

    model_calls: list[str]


def frozen_harness(
    *,
    target: Path,
    engine: Engine,
    services: Services,
    handlers: Mapping[str, Handler] | None = None,
) -> FrozenHarness:
    """`reset_test_repos(target)` of `tests/data` (a copy; `target` must not exist yet), with a model and a
    YouTube that record the attempt and raise."""
    repos = reset_test_repos(target)
    model_calls: list[str] = []
    raising = RaisingBackend(model_calls)

    def no_youtube(video_id: str) -> YoutubeFacts:
        raise ExternalCall(f"tests must not call YouTube ({video_id})")

    harness = FrozenHarness(
        ideas=repos["idea-bucket"],
        docs=repos["epiaku-docs"],
        engine=engine,
        services=services,
        fetcher=no_youtube,
        backends=SimpleNamespace(note=raising, chat=raising),
        handlers=handlers,
    )
    harness.model_calls = model_calls
    return harness
