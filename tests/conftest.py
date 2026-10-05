import json
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from catcher.core.config import Settings
from catcher.core.frontmatter import Doc, dump
from catcher.modules.llm.backends.fake import FakeBackend
from catcher.modules.llm.profiles import Profile, ProfilesConfig
from catcher.modules.llm.service import LlmResult, Usage
from catcher.modules.pipeline.doctypes import DOC_TYPES
from catcher.modules.pipeline.inbox import Note
from catcher.modules.pipeline.process import Services
from catcher.modules.pipeline.tags import load_tags
from catcher.modules.youtube.facts import FactsFetcher, FactsUnavailable, YoutubeFacts

FIXTURES = Path(__file__).parent / "fixtures"


pytest_plugins = ["blocknet"]  # the network guard: on for every run, off only with CATCHER_ALLOW_NETWORK=1


@pytest.fixture(autouse=True)
def _ignore_the_real_dotenv(monkeypatch):
    """Tests must never read the developer's real .env (it holds API keys and machine paths)."""
    monkeypatch.setattr("catcher.cli.load_dotenv", lambda *args, **kwargs: False)


@pytest.fixture(autouse=True)
def _no_real_database(monkeypatch):
    """DATABASE_URL defaults to the developer's own database: a test that does not name its own database
    gets one nobody listens on, so it can never take a lock or change a row there."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://catcher:catcher@127.0.0.1:1/catcher")


@pytest.fixture
def no_run_lock(monkeypatch):
    """`catcher run pipeline` takes the worker's lock in Postgres first. The tests of its options and its
    output, which run without Docker, hold a stand-in instead; the lock itself is tested on a real database
    in tests/integration/db/test_run_pipeline_cli.py."""

    @contextmanager
    def held(settings):
        yield lambda: None

    monkeypatch.setattr("catcher.cli._run_lock", held)


@pytest.fixture
def prompt_tags() -> dict[str, list[str]]:
    return {
        "idea_types": ["app-idea", "tech-note"],
        "topics": ["automation", "ai-agents"],
        "projects": ["idea-catcher"],
    }


@pytest.fixture
def fake_profiles() -> ProfilesConfig:
    return ProfilesConfig(default="fake", profiles={"fake": Profile(backend="fake")})


@pytest.fixture
def make_note():
    def _make(
        doctype: str = "note",
        *,
        doc_id: str = "a7b2c9",
        body: str = "An idea.\n",
        root: Path | None = None,
        **fm: Any,
    ) -> Note:
        base = {
            "id": doc_id,
            "class": doctype,
            "captured": "2026-09-27",
            "source_file": f"inbox/notes/{doc_id}.md",
        }
        doc = Doc({**base, **fm}, body)
        path = (root / "inbox/notes" / f"{doc_id}.md") if root else Path("inbox/notes") / f"{doc_id}.md"
        if root:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump(doc), encoding="utf-8")
        return Note(doc_id, DOC_TYPES[doctype], doc, path)

    return _make


@pytest.fixture
def make_result():
    def _make(
        output: BaseModel,
        *,
        profile: str = "fake",
        backend: str = "fake",
        model: str = "fake",
        version: str = "note-1",
    ) -> LlmResult:
        return LlmResult(output, profile, backend, model, version, Usage(tokens_in=10, tokens_out=5), 1)

    return _make


def _profiles_for_tests() -> ProfilesConfig:
    return ProfilesConfig(
        default="notes",
        profiles={
            "notes": Profile(backend="fake"),
            "fake": Profile(backend="fake"),
            "clippings": Profile(backend="openai", model="gpt-test"),
            "youtube": Profile(backend="openai", model="gpt-test"),
        },
    )


def _no_network(vid: str) -> YoutubeFacts:
    raise FactsUnavailable(f"tests must not fetch facts for {vid}")


@pytest.fixture
def make_services():
    def _make(
        note_backend: FakeBackend | None = None,
        chat_backend: FakeBackend | None = None,
        facts: FactsFetcher | None = None,
    ) -> Services:
        notes = note_backend or FakeBackend()
        chats = chat_backend or FakeBackend()
        return Services(
            settings=Settings(),
            profiles=_profiles_for_tests(),
            backends=lambda p: chats if p.backend == "openai" else notes,
            tags=load_tags(),
            facts=facts or _no_network,
        )

    return _make


@pytest.fixture
def yt_facts() -> YoutubeFacts:
    """Facts for nGVZS_wUDGM, built from its saved yt-dlp info and auto-captions (no network)."""
    from catcher.modules.youtube import facts as facts_mod

    folder = FIXTURES / "youtube"
    info = json.loads((folder / "nGVZS_wUDGM.info.json").read_text())
    captions = facts_mod._parse_vtt_captions((folder / "nGVZS_wUDGM.en.vtt").read_text())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(facts_mod, "_extract", lambda *args, **kwargs: (info, captions))
        return facts_mod.fetch_facts("nGVZS_wUDGM", today=date(2026, 9, 30))
