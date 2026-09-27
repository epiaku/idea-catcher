import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.frontmatter import load
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import UsageLimitReached
from catcher.modules.pipeline.run import RunOptions, run_pipeline

REPO = Path(__file__).parents[3]
NOTES = "hugo/content/en/docs/idea-bucket/notes"
CLIPPING = "hugo/content/en/docs/idea-bucket/clipping"


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
            f"{CLIPPING}/_index.md": "---\ntitle: Clippings\n---\n",
        },
    )
    return SimpleNamespace(ideas=ideas, docs=docs, ideas_bare=ideas_bare, docs_bare=docs_bare)


def files(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): p.read_text()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def test_run_publishes_archives_and_commits(repos, make_services, sh):
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert report.counts() == {"published": 2}
    [note_page] = [p.name for p in (repos.docs / NOTES).glob("*.md") if p.name != "_index.md"]
    assert note_page.endswith("_fake-note.md")
    assert [p.name for p in (repos.docs / CLIPPING).glob("2026*.md")] == [
        "20260925_cf81e40b020519ef_fake-chat.md"
    ]
    assert not list((repos.ideas / "inbox").rglob("*.md"))
    assert not list((repos.ideas / "staging").glob("*"))
    assert (repos.ideas / "archive/clippings/cf81e40b020519ef.md").exists()
    assert report.committed == {"docs": True, "ideas": True}
    assert sh(repos.docs, "status", "--porcelain") == ""
    assert sh(repos.ideas, "status", "--porcelain") == ""
    assert sh(repos.docs, "log", "-1", "--format=%s").strip() == "idea-catcher: publish 2 page(s)"
    assert sh(repos.docs_bare, "log", "--format=%s", "main").strip() == "seed"


def test_push_updates_both_remotes(repos, make_services, sh):
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(push=True), make_services())
    assert report.pushed
    assert (
        sh(repos.docs_bare, "log", "-1", "--format=%s", "main").strip() == "idea-catcher: publish 2 page(s)"
    )
    assert sh(repos.ideas_bare, "log", "-1", "--format=%s", "main").strip().startswith("idea-catcher:")


def test_dry_run_touches_nothing(repos, make_services):
    before = (files(repos.ideas), files(repos.docs))
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(dry_run=True), make_services())
    assert report.counts() == {"would_publish": 2}
    assert (files(repos.ideas), files(repos.docs)) == before
    assert report.committed == {}


def test_invalid_output_fails_one_note_and_keeps_it_staged(repos, make_services, sh):
    chats = FakeBackend(["nope", "still nope"])
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    assert report.counts() == {"published": 1, "failed": 1}
    assert (repos.ideas / "staging/cf81e40b020519ef.md").exists()
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_usage_limit_defers_every_claude_note_but_not_free_notes(repos, make_services):
    (repos.ideas / "inbox/clippings/second.md").write_text(chat("925d9b0b4ca21b63"))
    chats = FakeBackend([UsageLimitReached("Claude AI usage limit reached", backend="claude-code")])
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    assert report.counts() == {"published": 1, "deferred": 2}
    assert len(chats.prompts) == 1
    assert {i.doc_id for i in report.items if i.status == "deferred"} == {
        "cf81e40b020519ef",
        "925d9b0b4ca21b63",
    }


def test_limit_processes_at_most_n_notes(repos, make_services):
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(limit=1), make_services())
    assert report.counts() == {"published": 1, "skipped": 1}
    assert len(list((repos.ideas / "staging").glob("*.md"))) == 1


def test_reclipped_chat_overwrites_its_page(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    (repos.ideas / "inbox/clippings/systeme again.md").write_text(
        chat("cf81e40b020519ef", "**You**\n\nlonger\n" * 3)
    )
    v2 = json.dumps({**CANNED["ai-chat"], "title": "Bundles v2"})
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=FakeBackend([v2])))
    pages = sorted((repos.docs / CLIPPING).glob("2026*.md"))
    assert [p.name for p in pages] == ["20260925_cf81e40b020519ef_bundles-v2.md"]
    assert load(pages[0]).fm["title"] == "Bundles v2"


def test_unrelated_docs_changes_are_not_committed(repos, make_services, sh):
    (repos.docs / "README.md").write_text("my own edit\n")
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert sh(repos.docs, "status", "--porcelain").splitlines() == [" M README.md"]


def test_cli_run_pipeline(repos, monkeypatch):
    monkeypatch.setenv("PROFILES_FILE", str(REPO / "profiles.yaml"))
    args = ["run", "pipeline", "--ideas", str(repos.ideas), "--docs", str(repos.docs), "--profile", "fake"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "published" in result.output
    assert "summary:" in result.output


YT_CLIP = (
    '---\nsource : "https://www.youtube.com/watch?v=MBPHU7aaklM&list=PL1&t=1s"\n'
    "created: 2026-09-25\n---\nclip\n"
)


def test_youtube_without_facts_is_deferred_then_published(repos, make_services, yt_facts):
    (repos.ideas / "inbox/clippings/yt.md").write_text(YT_CLIP)
    first = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert {i.doc_id: i.status for i in first.items}["MBPHU7aaklM"] == "deferred"
    assert (repos.ideas / "staging/MBPHU7aaklM.md").exists()

    second = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(facts=lambda vid: yt_facts))
    assert {i.doc_id: i.status for i in second.items}["MBPHU7aaklM"] == "published"
    assert (repos.ideas / "archive/youtube/MBPHU7aaklM.youtube.json").exists()
    assert list((repos.docs / "hugo/content/en/docs/idea-bucket/youtube").glob("20260925_MBPHU7aaklM_*.md"))
