"""Every LLM call of a run leaves a trace in `llm/` of the idea-bucket, committed with the run."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from run_on_worker import run_on_worker
from sqlalchemy import Engine

from catcher.core.config import Settings
from catcher.core.frontmatter import load
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import BackendUnavailable

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _database(pg_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_on_worker` runs the command's path (B5b) on the test database."""
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))


NOTES = "hugo/content/en/docs/idea-bucket/notes"
WEB_CLIPS = "hugo/content/en/docs/idea-bucket/web-clips"


def chat(chat_id: str, body: str = "**You**\n\nsell bundles?\n\n---\n\n**Gemini**\n\nYes.\n") -> str:
    return (
        f'---\nsource : "https://gemini.google.com/app/{chat_id}?is_sa=1"\n'
        f'created: 2026-09-25\ntags:\n  - "clippings"\n---\n{body}'
    )


@pytest.fixture
def repos(make_repo):
    ideas_bare, ideas = make_repo(
        "idea-bucket",
        {
            "inbox/notes/YouTube walks.md": "Create YouTube content walking around\n",
            "inbox/clippings/systeme.md": chat("cf81e40b020519ef"),
        },
    )
    docs_bare, docs = make_repo(
        "epiaku-docs",
        {
            f"{NOTES}/_index.md": "---\ntitle: Notes\n---\n",
            f"{WEB_CLIPS}/_index.md": "---\ntitle: Web clips\n---\n",
        },
    )
    return SimpleNamespace(ideas=ideas, docs=docs, ideas_bare=ideas_bare, docs_bare=docs_bare)


def archived(ideas: Path, sub: str, original: str) -> Path:
    """The archive copy of the document captured as `original`: it carries the calculated name."""
    found = [
        p
        for p in sorted((ideas / "archive" / sub).glob("*.md"))
        if load(p).fm.get("original_filename") == original
    ]
    assert len(found) == 1, f"archive/{sub}: {original!r} -> {[p.name for p in found]}"
    return found[0]


def trace_of(ideas: Path, sub: str, original: str) -> Path:
    return ideas / "llm" / sub / archived(ideas, sub, original).with_suffix(".json").name


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def traces(ideas: Path) -> list[Path]:
    return sorted((ideas / "llm").rglob("*.json")) if (ideas / "llm").is_dir() else []


def test_a_run_writes_a_trace_per_document_and_commits_it(repos, make_services, sh):
    report = run_on_worker(repos, make_services())
    assert report.counts() == {"published": 2}
    assert len(traces(repos.ideas)) == 2
    note = read(trace_of(repos.ideas, "notes", "YouTube walks.md"))
    clip = read(trace_of(repos.ideas, "clippings", "systeme.md"))
    assert note["task"] == "note" and note["outcome"] == "ok" and note["output"]["title"] == "Fake Note"
    assert clip["task"] == "ai-chat" and clip["outcome"] == "ok" and len(clip["attempts"]) == 1
    assert clip["attempts"][0]["reply"] == json.dumps(CANNED["ai-chat"])
    tracked = sh(repos.ideas, "ls-files", "llm").splitlines()
    assert sorted(tracked) == sorted(p.relative_to(repos.ideas).as_posix() for p in traces(repos.ideas))
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_the_trace_name_matches_the_archive_and_output_name(repos, make_services):
    run_on_worker(repos, make_services())
    for sub, original in (("notes", "YouTube walks.md"), ("clippings", "systeme.md")):
        name = archived(repos.ideas, sub, original).name
        assert (repos.ideas / "output" / sub / name).exists()
        assert (repos.ideas / "llm" / sub / name).with_suffix(".json").exists()
        assert (repos.docs / (NOTES if sub == "notes" else WEB_CLIPS) / name).exists()


def test_a_requeue_overwrites_the_same_trace_file(repos, make_services, sh):
    run_on_worker(repos, make_services())
    path = trace_of(repos.ideas, "clippings", "systeme.md")
    first = path.read_text()
    assert read(path)["output"]["title"] == "Fake Chat"

    again = run_on_worker(repos, make_services())
    assert again.items == [] and path.read_text() == first  # not processed again: nothing new written

    v2 = json.dumps({**CANNED["ai-chat"], "title": "Bundles v2"})
    chats = FakeBackend([v2])
    report = run_on_worker(
        repos,  # a requeue alone reuses the saved reply
        make_services(chat_backend=chats),
        requeue=["systeme"],
        refresh_llm=True,
    )
    assert report.counts() == {"requeued": 1, "published": 1}
    assert read(path)["output"]["title"] == "Bundles v2"  # the same file, overwritten
    assert len(list((repos.ideas / "llm" / "clippings").glob("*.json"))) == 1
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_a_failed_llm_call_still_leaves_a_trace_with_the_error(repos, make_services, sh):
    chats = FakeBackend(["nope", "still nope"])
    report = run_on_worker(repos, make_services(chat_backend=chats))
    assert report.counts() == {"published": 1, "failed": 1}
    trace = read(trace_of(repos.ideas, "clippings", "systeme.md"))
    assert trace["outcome"] == "invalid_output" and trace["output"] is None
    assert [a["reply"] for a in trace["attempts"]] == ["nope", "still nope"]
    assert all(a["error"] for a in trace["attempts"]) and trace["error"]
    assert sh(repos.ideas, "status", "--porcelain") == ""

    (repos.ideas / "inbox/clippings/second.md").write_text(chat("925d9b0b4ca21b63"))
    down = FakeBackend([BackendUnavailable("the backend is down")])
    report = run_on_worker(repos, make_services(chat_backend=down))
    assert report.counts() == {"deferred": 1}
    trace = read(trace_of(repos.ideas, "clippings", "second.md"))
    assert trace["outcome"] == "backend_error" and trace["error"] == "the backend is down"
    assert trace["attempts"] == [] and trace["output"] is None
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_a_dry_run_writes_no_trace(repos, make_services):
    report = run_on_worker(repos, make_services(), dry_run=True)
    assert report.counts() == {"would_call_llm": 2}  # B52 (ruling): the preview never calls a model
    assert not (repos.ideas / "llm").exists()


def test_llm_trace_false_writes_nothing(repos, make_services, sh):
    services = make_services()
    services.settings = Settings(llm_trace=False)
    report = run_on_worker(repos, services)
    assert report.counts() == {"published": 2}
    assert not (repos.ideas / "llm").exists()
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_the_prompt_is_not_saved_by_default_but_is_with_llm_trace_prompt(repos, make_services):
    run_on_worker(repos, make_services(), only=["YouTube walks"])
    trace = read(trace_of(repos.ideas, "notes", "YouTube walks.md"))
    assert trace["prompt"] is None and len(trace["prompt_sha256"]) == 64

    services = make_services()
    services.settings = Settings(llm_trace_prompt=True)
    run_on_worker(repos, services, only=["systeme"])
    trace = read(trace_of(repos.ideas, "clippings", "systeme.md"))
    assert "sell bundles?" in trace["prompt"]


def test_an_unwritable_trace_folder_does_not_fail_the_document(repos, make_services, caplog):
    (repos.ideas / "llm").write_text("not a folder\n")
    caplog.set_level("WARNING", logger="catcher")
    report = run_on_worker(repos, make_services())
    assert report.counts() == {"published": 2}
    assert (repos.ideas / "llm").is_file()
    warnings = [r for r in caplog.records if r.name == "catcher.process" and r.levelname == "WARNING"]
    assert len(warnings) == 2 and all("trace" in r.getMessage() for r in warnings)


def test_the_trace_holds_no_secret(repos, make_services):
    services = make_services()
    services.settings = Settings(
        openai_api_key="sk-secret-123", freellmapi_api_key="free-secret-456", llm_trace_prompt=True
    )
    report = run_on_worker(repos, services)
    assert report.counts() == {"published": 2}
    written = [p for p in (repos.ideas / "llm").rglob("*") if p.is_file()]
    assert len(written) == 2
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert "sk-secret-123" not in text and "free-secret-456" not in text


def test_a_failed_requeue_keeps_the_paid_replies_of_the_first_run(repos, make_services):
    run_on_worker(repos, make_services())
    path = trace_of(repos.ideas, "clippings", "systeme.md")
    first = path.read_bytes()
    assert read(path)["outcome"] == "ok"

    down = FakeBackend([BackendUnavailable("the backend is down")])
    report = run_on_worker(
        repos,  # a requeue alone reuses the saved reply
        make_services(chat_backend=down),
        requeue=["systeme"],
        refresh_llm=True,
    )
    assert report.counts().get("deferred") == 1
    assert path.read_bytes() == first
    assert read(path)["attempts"][0]["reply"] == json.dumps(CANNED["ai-chat"])
