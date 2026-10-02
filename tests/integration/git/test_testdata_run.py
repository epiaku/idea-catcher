"""The whole pipeline on the committed test data: a frozen real run.

`tests/data/idea-bucket` holds the user's real captures plus what a real run recorded: the YouTube facts in
`facts/` and the LLM replies in `llm/`. A run reads those before it calls YouTube or a model, so the whole
pipeline runs here with no external call, and every page must equal the page the user approved, kept in
`tests/data/expected/` (which `catcher testdata reset` does not copy).
"""

import shutil
from pathlib import Path

from catcher.core.frontmatter import load
from catcher.core.testdata import DEFAULT_SOURCE, reset_test_repos
from catcher.modules.llm.service import TransientBackendError
from catcher.modules.pipeline.run import RunOptions, run_pipeline
from catcher.modules.youtube.access import YoutubeAccess
from catcher.modules.youtube.gate import YoutubeGate

EXPECTED = DEFAULT_SOURCE / "expected"
PAGES = Path("hugo/content/en/docs/idea-bucket")

# Frontmatter that differs on every run by design, and only that:
# - source_file: the calculated name, `YYYYMMDD-<random 6 hex>-<title>`, on every page;
# - id: a note's id is drawn again on every scan (a clip's id comes from its source, so it is compared);
# - date: a note has no capture date, so it gets the day of the run (a clip's date is its capture date).
VOLATILE = frozenset({"source_file"})
VOLATILE_IN_NOTES = frozenset({"id", "date"})


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
    gate = YoutubeGate(tmp_path / "gate", min_gap_s=600, jitter_s=0, block_hours=6)
    services.youtube = YoutubeAccess(no_youtube, gate, wait_max_s=0)
    return services


def published_pages(folder: Path) -> dict[str, tuple[Path, dict, str]]:
    """Every page below `folder`, by the name it was captured under (`original_filename`)."""
    pages: dict[str, tuple[Path, dict, str]] = {}
    for path in sorted(folder.rglob("*.md")):
        if path.name == "_index.md":
            continue
        doc = load(path)
        name = doc.fm["original_filename"]
        assert name not in pages, f"two pages for {name}: {pages[name][0]} and {path}"
        pages[name] = (path.relative_to(folder), doc.fm, doc.body)
    return pages


def stable(fm: dict, section: Path) -> dict:
    volatile = VOLATILE | VOLATILE_IN_NOTES if section == Path("notes") else VOLATILE
    return {k: v for k, v in fm.items() if k not in volatile}


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

    report = run_pipeline(ideas, docs, RunOptions(), services)

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

    expected, actual = published_pages(EXPECTED), published_pages(docs / PAGES)
    assert sorted(actual) == sorted(expected)
    for name, (rel, fm, body) in actual.items():
        want_rel, want_fm, want_body = expected[name]
        assert rel.parent == want_rel.parent, name  # the same section (notes/, clippings/, youtube/, ...)
        assert stable(fm, rel.parent) == stable(want_fm, want_rel.parent), name
        assert body == want_body, name


def test_deleting_the_saved_replies_would_call_the_model(tmp_path, make_services, sh):
    """Without `llm/` the same run calls the model: the test above passes because of the saved replies."""
    repos = reset_test_repos(tmp_path / "ic")
    ideas, docs = repos["idea-bucket"], repos["epiaku-docs"]
    shutil.rmtree(ideas / "llm")
    sh(ideas, "commit", "-qam", "no saved replies")
    model_calls: list[str] = []
    youtube_calls: list[str] = []
    services = offline_services(make_services, tmp_path, model_calls, youtube_calls)

    report = run_pipeline(ideas, docs, RunOptions(), services)

    counts = report.counts()
    assert model_calls  # the model was asked (and refused)
    assert youtube_calls == []  # the saved facts are still there
    assert counts.get("published", 0) == 0
    assert counts["artifact"] == 1  # an artifact needs no model
    assert set(counts) <= {"artifact", "deferred", "failed"}
    assert not list((docs / PAGES).rglob("2*.md"))  # no page at all
