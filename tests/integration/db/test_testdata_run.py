"""The whole pipeline on the committed test data: a frozen real run.

`tests/data/idea-bucket` holds the user's real captures plus what a real run recorded: the YouTube facts in
`facts/` and the LLM replies in `llm/`. A run reads those before it calls YouTube or a model, so the whole
pipeline runs here with no external call, and every page must equal the page the user approved, kept in
`tests/data/expected/` (which `catcher testdata reset` does not copy).

The run goes through the worker path (`run_on_worker`, what `catcher run pipeline` does; B5b), on a copy of
the data, so it needs the test database.
"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from golden import EXPECTED, PAGES, compare_pages
from memory_gate import InMemoryGate
from run_on_worker import run_on_worker
from sqlalchemy import Engine

from catcher.core.frontmatter import load
from catcher.core.testdata import reset_test_repos
from catcher.modules.llm.service import TransientBackendError
from catcher.modules.youtube.access import YoutubeAccess

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _database(pg_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))


class ExternalCall(AssertionError):
    pass


def offline_services(make_services, tmp_path: Path, model_calls: list[str], youtube_calls: list[str]):
    """The real profile names, but a model and a YouTube that must not be called: they record and raise."""

    class RaisingBackend:
        name = "raising"

        def complete(self, prompt: str, *, model: str | None, task: str):
            model_calls.append(task)
            raise TransientBackendError(f"tests must not call a model ({task})")

    def no_youtube(vid: str):
        youtube_calls.append(vid)
        raise ExternalCall(f"tests must not call YouTube ({vid})")

    services = make_services(facts=no_youtube)
    services.backends = lambda profile: RaisingBackend()
    gate = InMemoryGate(min_gap_s=600, jitter_s=0, block_hours=6)
    services.youtube = YoutubeAccess(no_youtube, gate, wait_max_s=0)
    return services


def file_bytes(folder: Path) -> dict[Path, bytes]:
    return {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_the_whole_pipeline_on_the_committed_test_data(tmp_path, make_services, sh):
    """43 real captures and an artifact, published from the saved facts and replies alone."""
    repos = reset_test_repos(tmp_path / "ic")
    ideas, docs = repos["idea-bucket"], repos["epiaku-docs"]
    traces_before, facts_before = file_bytes(ideas / "llm"), file_bytes(ideas / "facts")
    model_calls: list[str] = []
    youtube_calls: list[str] = []
    services = offline_services(make_services, tmp_path, model_calls, youtube_calls)

    report = run_on_worker(SimpleNamespace(ideas=ideas, docs=docs), services)

    problems = [f"{i.doc_id} {i.status}: {i.message}" for i in report.items if i.status != "published"]
    assert report.counts() == {"published": 43, "artifact": 1}, problems
    assert not report.unreadable and not report.not_found and not report.problems
    assert model_calls == [] and youtube_calls == []
    assert not [p for p in (ideas / "inbox").rglob("*") if p.is_file()]  # everything was worked on
    assert sh(ideas, "status", "--porcelain") == "" and sh(docs, "status", "--porcelain") == ""

    assert file_bytes(ideas / "llm") == traces_before  # read, never rewritten, and no new trace
    assert file_bytes(ideas / "facts") == facts_before

    [archived_pdf] = list((ideas / "archive/artifacts").iterdir())
    assert archived_pdf.name.endswith("-sample-report.pdf")
    assert (docs / "idea-bucket/artifacts" / archived_pdf.name).exists()

    [web_clip_page] = list((docs / PAGES / "web-clips").glob("*hugo-shortcodes-explained.md"))
    web_clip = load(web_clip_page).fm
    assert web_clip["id"] and web_clip["source"] == "https://gohugo.io/content-management/shortcodes/"

    compare_pages(docs / PAGES, EXPECTED)


def test_deleting_the_saved_replies_would_call_the_model(tmp_path, make_services, sh):
    """Without `llm/` the same run calls the model: the test above passes because of the saved replies."""
    repos = reset_test_repos(tmp_path / "ic")
    ideas, docs = repos["idea-bucket"], repos["epiaku-docs"]
    shutil.rmtree(ideas / "llm")
    sh(ideas, "commit", "-qam", "no saved replies")
    model_calls: list[str] = []
    youtube_calls: list[str] = []
    services = offline_services(make_services, tmp_path, model_calls, youtube_calls)

    report = run_on_worker(SimpleNamespace(ideas=ideas, docs=docs), services)

    counts = report.counts()
    assert model_calls  # the model was asked (and refused)
    assert youtube_calls == []  # the saved facts are still there
    assert counts.get("published", 0) == 0
    assert counts["artifact"] == 1  # an artifact needs no model
    assert set(counts) <= {"artifact", "deferred", "failed"}
    assert not list((docs / PAGES).rglob("2*.md"))  # no page at all
