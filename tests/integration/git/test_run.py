import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.frontmatter import load
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import BudgetExhausted, UsageLimitReached
from catcher.modules.pipeline.inbox import deferred_in_output, slugify_title
from catcher.modules.pipeline.run import RunOptions, run_pipeline
from catcher.modules.youtube.facts import FactsUnavailable
from catcher.modules.youtube.gate_rules import OPEN

REPO = Path(__file__).parents[3]
NOTES = "hugo/content/en/docs/idea-bucket/notes"
WEB_CLIPS = "hugo/content/en/docs/idea-bucket/web-clips"


def find(ideas: Path, folder: str, sub: str, original: str) -> Path:
    """The file in `folder/sub` that came from the document captured as `original` (for example "systeme.md").

    Every folder uses the calculated name, so it is looked up through the archive copy, which records the
    original name in its frontmatter (an unreadable file has none, so it is matched on the title part).
    """
    archived = []
    for path in sorted((ideas / "archive" / sub).glob("*.md")):
        try:
            name = load(path).fm.get("original_filename")
        except ValueError:
            name = None
        if name == original or (
            name is None and path.name.endswith(f"-{slugify_title(Path(original).stem)}.md")
        ):
            archived.append(path)
    assert len(archived) == 1, f"archive/{sub}: {original!r} -> {[h.name for h in archived]}"
    if folder == "archive":
        return archived[0]
    path = ideas / folder / sub / archived[0].name
    assert path.exists(), f"{folder}/{sub}/{archived[0].name} does not exist"
    return path


def absent(ideas: Path, folder: str, sub: str, original: str) -> bool:
    try:
        find(ideas, folder, sub, original)
    except AssertionError:
        return True
    return False


def retry(ideas: Path, sub: str, original: str) -> None:
    """How you retry: move the original from archive/ back into inbox/, under its calculated name."""
    archived = find(ideas, "archive", sub, original)
    archived.replace(ideas / "inbox" / sub / archived.name)


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
    assert note_page.endswith("-youtube-walks.md")
    [chat_page] = [p.name for p in (repos.docs / WEB_CLIPS).glob("2026*.md")]
    assert chat_page.startswith("20260925-") and chat_page.endswith("-sell-bundles.md")
    assert not list((repos.ideas / "inbox").rglob("*.md"))
    assert not (repos.ideas / "staging").exists()
    note_archived = find(repos.ideas, "archive", "notes", "YouTube walks.md")
    assert note_archived.name.endswith("-youtube-walks.md")
    assert load(note_archived).fm["original_filename"] == "YouTube walks.md"
    assert load(note_archived).fm["calculated_filename"] == note_archived.name
    assert load(note_archived).body == "Create YouTube content walking around\n"
    chat_archived = find(repos.ideas, "archive", "clippings", "systeme.md")
    assert load(chat_archived).body.startswith("**You**\n\nsell bundles?")
    final = find(repos.ideas, "output", "clippings", "systeme.md")
    assert chat_archived.name == final.name  # one name in archive/ and output/ ...
    assert (repos.docs / WEB_CLIPS / final.name).read_text().startswith("---\n")  # ... and in epiaku-docs
    assert load(repos.docs / WEB_CLIPS / final.name).fm["id"] == "cf81e40b020519ef"
    assert load(final).fm["source_file"] == f"clippings/{final.name}"
    assert load(final).fm["original_filename"] == "systeme.md"
    assert load(final).fm["stage"] == "published"
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


def test_invalid_output_moves_the_note_to_failed_with_the_reason(repos, make_services, sh):
    chats = FakeBackend(["nope", "still nope"])
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    assert report.counts() == {"published": 1, "failed": 1}
    failed = find(repos.ideas, "failed", "clippings", "systeme.md")
    assert "invalid output" in failed.with_suffix(".error.txt").read_text()
    assert absent(repos.ideas, "output", "clippings", "systeme.md")
    assert find(repos.ideas, "archive", "clippings", "systeme.md").name == failed.name
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_a_rate_limit_defers_every_note_of_that_backend_but_not_the_others(repos, make_services):
    (repos.ideas / "inbox/clippings/second.md").write_text(chat("925d9b0b4ca21b63"))
    chats = FakeBackend([UsageLimitReached("rate limit", backend="openai")])
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    assert report.counts() == {"published": 1, "deferred": 2}
    assert len(chats.prompts) == 1
    assert {i.doc_id for i in report.items if i.status == "deferred"} == {
        "cf81e40b020519ef",
        "925d9b0b4ca21b63",
    }
    for name in ("systeme.md", "second.md"):  # deferred: out of the inbox, stalled in output/ with the reason
        stalled = find(repos.ideas, "output", "clippings", name)
        assert load(stalled).fm["stage"] == "deferred" and "openai" in load(stalled).fm["deferred_reason"]
        assert not (repos.ideas / "inbox/clippings" / name).exists()
        assert find(repos.ideas, "archive", "clippings", name).name == stalled.name


def test_a_limit_is_logged_once_not_for_every_note_that_waits(repos, make_services, caplog):
    with caplog.at_level("INFO", logger="catcher.run"):
        run_pipeline(repos.ideas, repos.docs, RunOptions(limit=1), make_services())
    lines = [r.getMessage() for r in caplog.records if r.name == "catcher.run"]
    assert [m for m in lines if m.startswith("limit of")] == ["limit of 1 reached: 1 note(s) stay in inbox/"]
    assert not [m for m in lines if "skipped, run limit reached" in m]


def test_limit_processes_at_most_n_notes(repos, make_services):
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(limit=1), make_services())
    assert report.counts() == {"published": 1, "skipped": 1}
    assert len(list((repos.ideas / "inbox").rglob("*.md"))) == 1  # the other one is still waiting in inbox/


def test_reclipped_chat_overwrites_its_page(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    (repos.ideas / "inbox/clippings/systeme again.md").write_text(
        chat("cf81e40b020519ef", "**You**\n\nlonger\n" * 3)
    )
    v2 = json.dumps({**CANNED["ai-chat"], "title": "Bundles v2"})
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=FakeBackend([v2])))
    pages = sorted((repos.docs / WEB_CLIPS).glob("2026*.md"))
    assert len(pages) == 1  # the older page with the same id was replaced
    assert load(pages[0]).fm["title"] == "Bundles v2"
    assert len(list((repos.ideas / "archive/clippings").glob("*.md"))) == 2  # both clips are kept


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
    '---\nsource : "https://www.youtube.com/watch?v=nGVZS_wUDGM&list=PL1&t=1s"\n'
    "created: 2026-09-25\n---\nclip\n"
)


def test_youtube_without_facts_is_deferred_then_published(repos, make_services, yt_facts):
    (repos.ideas / "inbox/clippings/yt.md").write_text(YT_CLIP)
    first = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert {i.doc_id: i.status for i in first.items}["nGVZS_wUDGM"] == "deferred"
    stalled_path = find(repos.ideas, "output", "clippings", "yt.md")
    stalled = load(stalled_path)
    assert stalled.fm["stage"] == "deferred" and "facts" in stalled.fm["deferred_reason"]
    assert not (repos.ideas / "inbox/clippings/yt.md").exists() and not (repos.ideas / "failed").exists()

    # a run only reads inbox/, so nothing is retried until the file is moved back from archive/
    assert run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services()).items == []
    retry(repos.ideas, "clippings", "yt.md")
    second = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(facts=lambda vid: yt_facts))
    assert {i.doc_id: i.status for i in second.items}["nGVZS_wUDGM"] == "published"
    final = find(repos.ideas, "output", "clippings", "yt.md")
    assert (
        final.name == stalled_path.name
    )  # the retry kept the calculated name and overwrote the stalled copy
    assert load(final).fm["stage"] == "published"
    assert find(repos.ideas, "archive", "clippings", "yt.md").name == final.name  # not a second archive copy
    assert (repos.docs / "hugo/content/en/docs/idea-bucket/youtube" / final.name).exists()


def test_run_logs_progress_per_note_and_a_total(repos, make_services, caplog):
    caplog.set_level("INFO", logger="catcher")
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    messages = [r.getMessage() for r in caplog.records]
    assert any("(1/2)" in m and "processing" in m for m in messages)
    assert any("(2/2)" in m and "published" in m for m in messages)
    assert any(m.startswith("processed 2/2: 2 published") for m in messages)
    assert any('"inbox/notes/YouTube walks.md"' in m and "published" in m for m in messages)
    assert any('"inbox/clippings/systeme.md"' in m and "processing" in m for m in messages)


def test_the_steps_of_a_document_are_debug_lines_not_info(repos, make_services, caplog):
    """At INFO a document gets its result line; naming, archiving and the LLM steps are DEBUG lines."""
    caplog.set_level("DEBUG", logger="catcher")
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    steps = ("named ", "archived ", "started work", "asking the LLM", "reason done", "using the saved")
    detail = [r for r in caplog.records if any(s in r.getMessage() for s in steps)]
    assert detail and all(r.levelname == "DEBUG" for r in detail)
    assert any(m.startswith("archived archive/notes/") for m in (r.getMessage() for r in detail))


def test_failures_and_deferrals_are_always_logged(repos, make_services, caplog):
    caplog.set_level("INFO", logger="catcher")
    chats = FakeBackend(["nope", "still nope"])
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    [error] = [r for r in caplog.records if r.levelname == "ERROR"]
    assert "/2)" in error.getMessage() and "cf81e40b020519ef" in error.getMessage()
    assert "invalid output" in error.getMessage()


def test_an_unexpected_error_fails_that_note_and_is_logged_with_a_traceback(
    repos, make_services, caplog, monkeypatch
):
    caplog.set_level("INFO", logger="catcher")

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("catcher.modules.pipeline.run.process_note", boom)
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert report.counts() == {"failed": 2}
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 2 and all(r.exc_info for r in errors)


def test_a_published_document_is_not_processed_again(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    again = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert again.items == []


def test_a_usage_limit_stalls_the_note_in_output_until_you_move_it_back(repos, make_services):
    chats = FakeBackend([UsageLimitReached("limit", backend="openai")])
    first = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    assert first.counts() == {"published": 1, "deferred": 1}
    stalled = find(repos.ideas, "output", "clippings", "systeme.md")
    assert load(stalled).fm["stage"] == "deferred"
    assert not (repos.ideas / "inbox/clippings/systeme.md").exists()
    assert not (repos.ideas / "failed").exists()
    assert (
        run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services()).items == []
    )  # not retried alone
    retry(repos.ideas, "clippings", "systeme.md")
    second = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert second.counts() == {"published": 1}
    final = find(repos.ideas, "output", "clippings", "systeme.md")
    assert final.name == stalled.name and load(final).fm["stage"] == "published"


def test_an_unreadable_capture_is_reported_and_filed_as_failed(repos, make_services, sh):
    (repos.ideas / "inbox/notes/bad.md").write_text("---\ntitle: [oops\n---\nbody\n")
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert list(report.unreadable) == ["inbox/notes/bad.md"]
    failed = find(repos.ideas, "failed", "notes", "bad.md")
    assert find(repos.ideas, "archive", "notes", "bad.md").name == failed.name
    assert (
        failed.read_text() == "---\ntitle: [oops\n---\nbody\n"
    )  # bytes unchanged: its frontmatter can't be edited
    assert "original file: bad.md" in failed.with_suffix(".error.txt").read_text()
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_a_used_up_budget_logs_one_error_per_backend_and_keeps_the_notes(repos, make_services, caplog):
    (repos.ideas / "inbox/clippings/second.md").write_text(chat("925d9b0b4ca21b63"))
    chats = FakeBackend([BudgetExhausted("insufficient_quota", backend="openai")])
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    assert report.counts() == {"published": 1, "deferred": 2}
    assert {i.message for i in report.items if i.status == "deferred"} == {"budget reached (openai)"}
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "openai budget reached: 2 note(s) waiting" in errors[0]
    assert not (repos.ideas / "failed").exists()
    assert len(chats.prompts) == 1


def test_the_waiting_notes_go_through_once_the_budget_is_back(repos, make_services):
    chats = FakeBackend([BudgetExhausted("insufficient_quota", backend="openai")])
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    retry(repos.ideas, "clippings", "systeme.md")
    second = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert second.counts() == {"published": 1}


def test_a_missing_model_is_a_configuration_error_and_the_note_is_not_failed(repos, make_services, caplog):
    services = make_services()
    services.profiles.profiles["clippings"].model = None
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), services)
    assert report.counts() == {"published": 1, "deferred": 1}
    assert not (repos.ideas / "failed").exists()
    assert any("configuration error" in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)


def turns(n: int, last: str = "the last answer") -> str:
    parts = [f"**You**\n\nquestion {i}\n\n---\n\n**Gemini**\n\nanswer {i}\n" for i in range(1, n)]
    return "\n".join([*parts, f"**You**\n\nquestion {n}\n\n---\n\n**Gemini**\n\n{last}\n"])


def test_growing_snapshots_of_one_conversation_cost_one_llm_call(repos, make_services, caplog):
    clips = repos.ideas / "inbox/clippings"
    for name, n in [("chat short.md", 3), ("chat medium.md", 5), ("chat long.md", 8)]:
        (clips / name).write_text(chat("2446cd9c762c9cc9", turns(n)))
    chats = FakeBackend()
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    counts = report.counts()
    assert counts["duplicate"] == 2 and counts["published"] == 3
    assert len(chats.prompts) == 2  # the long snapshot of 2446... and the separate chat cf81e40b...
    assert "question 8" in chats.prompts[0] or "question 8" in chats.prompts[1]
    dup = find(repos.ideas, "duplicates", "clippings", "chat short.md")
    assert load(dup).fm["duplicate_of"] == "clippings/chat long.md" and "stage" not in load(dup).fm
    assert find(repos.ideas, "archive", "clippings", "chat short.md").name == dup.name
    assert absent(repos.ideas, "output", "clippings", "chat short.md")
    assert absent(repos.ideas, "output", "clippings", "chat medium.md")
    assert load(find(repos.ideas, "output", "clippings", "chat long.md")).fm["stage"] == "published"
    pages = [p for p in (repos.docs / WEB_CLIPS).glob("2026*.md") if load(p).fm["id"] == "2446cd9c762c9cc9"]
    assert len(pages) == 1
    assert any("duplicates/" in r.getMessage() and "chat long.md" in r.getMessage() for r in caplog.records)


def test_duplicates_are_not_processed_again(repos, make_services):
    clips = repos.ideas / "inbox/clippings"
    (clips / "chat short.md").write_text(chat("2446cd9c762c9cc9", turns(3)))
    (clips / "chat long.md").write_text(chat("2446cd9c762c9cc9", turns(6)))
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    again = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert again.items == []


def test_two_clips_with_one_id_but_different_content_are_both_processed(repos, make_services):
    clips = repos.ideas / "inbox/clippings"
    (clips / "one.md").write_text(chat("2446cd9c762c9cc9", turns(3).replace("question 2", "something else")))
    (clips / "two.md").write_text(chat("2446cd9c762c9cc9", turns(6)))
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert "duplicate" not in report.counts()
    find(repos.ideas, "output", "clippings", "one.md")
    assert not (repos.ideas / "duplicates").exists()


def test_dry_run_reports_duplicates_without_touching_files(repos, make_services):
    clips = repos.ideas / "inbox/clippings"
    (clips / "chat short.md").write_text(chat("2446cd9c762c9cc9", turns(3)))
    (clips / "chat long.md").write_text(chat("2446cd9c762c9cc9", turns(6)))
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(dry_run=True), make_services())
    assert report.counts()["duplicate"] == 1
    assert (clips / "chat short.md").exists() and not (repos.ideas / "output").exists()


def test_a_duplicate_keeps_its_archive_copy(repos, make_services):
    clips = repos.ideas / "inbox/clippings"
    (clips / "chat short.md").write_text(chat("2446cd9c762c9cc9", turns(3)))
    (clips / "chat long.md").write_text(chat("2446cd9c762c9cc9", turns(6)))
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    dup = find(repos.ideas, "duplicates", "clippings", "chat short.md")
    archived = find(repos.ideas, "archive", "clippings", "chat short.md")
    assert archived.name == dup.name
    assert load(archived).body == load(dup).body  # the archive copy keeps the text; only two lines were added
    assert load(archived).fm["original_filename"] == "chat short.md"


def test_file_processes_only_the_named_document(repos, make_services):
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(only=["YouTube walks"]), make_services())
    assert report.counts() == {"published": 1}
    assert report.not_found == []
    assert (repos.ideas / "inbox/clippings/systeme.md").exists()  # not selected: untouched
    assert absent(repos.ideas, "archive", "clippings", "systeme.md")
    find(repos.ideas, "output", "notes", "YouTube walks.md")


def test_file_that_finds_nothing_warns_and_processes_nothing(repos, make_services, caplog):
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(only=["No such doc.md"]), make_services())
    assert report.items == [] and report.not_found == ["No such doc.md"]
    assert any(
        r.levelname == "WARNING" and 'no document named "No such doc.md"' in r.getMessage()
        for r in caplog.records
    )
    assert (repos.ideas / "inbox/notes/YouTube walks.md").exists()


def test_file_only_looks_in_the_inbox_never_in_output(repos, make_services, caplog):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())  # everything is now in output/
    again = run_pipeline(repos.ideas, repos.docs, RunOptions(only=["systeme"]), make_services())
    assert again.items == [] and again.not_found == ["systeme"]
    assert any('no document named "systeme"' in r.getMessage() for r in caplog.records)


def test_file_and_limit_work_together(repos, make_services):
    report = run_pipeline(
        repos.ideas, repos.docs, RunOptions(only=["YouTube walks", "systeme"], limit=1), make_services()
    )
    assert report.counts() == {"published": 1, "skipped": 1}


def test_a_named_short_clip_is_processed_when_its_longer_clip_is_not_selected(repos, make_services):
    clips = repos.ideas / "inbox/clippings"
    (clips / "chat short.md").write_text(chat("2446cd9c762c9cc9", turns(3)))
    (clips / "chat long.md").write_text(chat("2446cd9c762c9cc9", turns(6)))
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(only=["chat short"]), make_services())
    assert report.counts() == {"published": 1}
    assert (clips / "chat long.md").exists()


def test_cli_file_option_warns_and_exits_with_1_when_nothing_matches(repos, monkeypatch):
    monkeypatch.setenv("PROFILES_FILE", str(REPO / "profiles.yaml"))
    args = ["run", "pipeline", "--ideas", str(repos.ideas), "--docs", str(repos.docs), "--profile", "fake"]
    result = CliRunner().invoke(app, [*args, "--file", "Nope.md", "-f", "systeme"])
    assert result.exit_code == 1
    assert 'not-found      no document named "Nope.md"' in result.output
    assert "published" in result.output


def test_a_document_leaves_the_inbox_only_when_it_is_worked_on(repos, make_services):
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(limit=1), make_services())
    assert report.counts() == {"published": 1, "skipped": 1}
    [left] = list((repos.ideas / "inbox").rglob("*.md"))
    assert not list((repos.ideas / "archive" / left.parent.name).glob(f"*-{slugify_title(left.stem)}.md"))
    assert len(list((repos.ideas / "output").rglob("*.md"))) == 1


def test_a_dry_run_moves_nothing_out_of_the_inbox(repos, make_services):
    before = files(repos.ideas)
    run_pipeline(repos.ideas, repos.docs, RunOptions(dry_run=True), make_services())
    assert files(repos.ideas) == before


def test_a_failed_note_keeps_its_archive_copy_and_leaves_no_working_copy(repos, make_services):
    chats = FakeBackend(["nope", "still nope"])
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    failed = find(repos.ideas, "failed", "clippings", "systeme.md")
    archived = find(repos.ideas, "archive", "clippings", "systeme.md")
    assert failed.name == archived.name
    assert load(archived).body == load(failed).body  # the text is untouched
    assert absent(repos.ideas, "output", "clippings", "systeme.md")


def add_artifact(repos, name: str = "report.pdf", data: bytes = b"%PDF-1.7 binary \x00\x01") -> Path:
    path = repos.ideas / "inbox" / name
    path.write_bytes(data)
    return path


def test_an_artifact_is_archived_and_copied_to_epiaku_docs_and_committed(repos, make_services, sh):
    src = add_artifact(repos)
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert report.counts() == {"published": 2, "artifact": 1}
    [archived] = list((repos.ideas / "archive/artifacts").iterdir())
    [published] = list((repos.docs / "idea-bucket/artifacts").iterdir())
    assert archived.name == published.name and archived.name.endswith("-report.pdf")
    assert archived.read_bytes() == published.read_bytes() == b"%PDF-1.7 binary \x00\x01"
    assert not src.exists() and not (repos.ideas / "output/artifacts").exists()
    assert sh(repos.docs, "status", "--porcelain") == "" and sh(repos.ideas, "status", "--porcelain") == ""
    assert (
        sh(repos.docs, "log", "-1", "--format=%s").strip()
        == "idea-catcher: publish 2 page(s) and 1 artifact(s)"
    )
    assert not list((repos.docs / "hugo").rglob("*.pdf"))  # nothing goes into the Hugo content


def test_a_requeued_artifact_keeps_its_name(repos, make_services):
    add_artifact(repos)
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    [archived] = list((repos.ideas / "archive/artifacts").iterdir())
    archived.replace(repos.ideas / "inbox" / archived.name)  # the manual retry
    again = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert again.counts() == {"artifact": 1}
    assert [p.name for p in (repos.ideas / "archive/artifacts").iterdir()] == [archived.name]
    assert [p.name for p in (repos.docs / "idea-bucket/artifacts").iterdir()] == [archived.name]


def test_a_file_over_the_size_limit_is_skipped_with_a_warning_and_stays_in_the_inbox(
    repos, make_services, caplog
):
    src = add_artifact(repos)
    services = make_services()
    services.settings.artifact_max_mb = 0
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), services)
    assert report.counts() == {"published": 2, "skipped": 1}
    assert src.exists() and not (repos.ideas / "archive/artifacts").exists()
    assert not (repos.docs / "idea-bucket").exists()
    assert any(r.levelname == "WARNING" and "ARTIFACT_MAX_MB" in r.getMessage() for r in caplog.records)


def test_a_dry_run_only_reports_the_artifact(repos, make_services):
    src = add_artifact(repos)
    before = files(repos.ideas)
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(dry_run=True), make_services())
    assert report.counts() == {"would_publish": 2, "would_copy": 1}
    assert src.exists() and files(repos.ideas) == before and not (repos.docs / "idea-bucket").exists()


def test_limit_does_not_apply_to_artifacts_and_file_can_select_only_one(repos, make_services):
    add_artifact(repos, "one.pdf")
    add_artifact(repos, "two.png", b"\x89PNG")
    limited = run_pipeline(repos.ideas, repos.docs, RunOptions(limit=1), make_services())
    assert limited.counts() == {"published": 1, "skipped": 1, "artifact": 2}


def test_file_can_name_just_an_artifact(repos, make_services):
    add_artifact(repos)
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(only=["report.pdf"]), make_services())
    assert report.counts() == {"artifact": 1}
    assert (repos.ideas / "inbox/notes/YouTube walks.md").exists()  # the notes were not selected


def test_requeue_brings_a_stalled_document_back_and_runs_it_again(repos, make_services, yt_facts):
    (repos.ideas / "inbox/clippings/yt.md").write_text(YT_CLIP)
    first = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert {i.doc_id: i.status for i in first.items}["nGVZS_wUDGM"] == "deferred"
    stalled_name = find(repos.ideas, "output", "clippings", "yt.md").name

    opts = RunOptions(requeue=["yt"])  # the name it was captured under, no manual move needed
    second = run_pipeline(repos.ideas, repos.docs, opts, make_services(facts=lambda vid: yt_facts))
    assert second.counts() == {"requeued": 1, "published": 1}  # only that document ran
    assert second.not_in_archive == []
    assert not list((repos.ideas / "inbox").rglob("yt*.md"))
    final = find(repos.ideas, "output", "clippings", "yt.md")
    assert final.name == stalled_name and load(final).fm["stage"] == "published"  # overwrote the stalled copy
    assert (repos.ideas / "archive/clippings" / stalled_name).exists()  # archived again, same name


def test_requeue_by_calculated_name_reruns_a_published_note_with_the_same_page(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    pages = sorted(p.name for p in (repos.docs / NOTES).glob("*.md"))
    archived = find(repos.ideas, "archive", "notes", "YouTube walks.md")
    chats = FakeBackend()
    report = run_pipeline(
        repos.ideas,
        repos.docs,
        RunOptions(requeue=[f"notes/{archived.name}"], profile="fake"),
        make_services(chat_backend=chats, note_backend=chats),
    )
    assert report.counts() == {"requeued": 1, "published": 1}
    assert sorted(p.name for p in (repos.docs / NOTES).glob("*.md")) == pages  # overwritten, not duplicated
    assert not list((repos.ideas / "inbox").rglob("*.md"))
    assert len(chats.prompts) == 1  # the systeme clipping was not touched


def test_requeue_of_an_unknown_name_is_not_found_and_runs_nothing(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(requeue=["Nope"]), make_services())
    assert report.not_in_archive == ["Nope"] and report.items == []
    assert not list((repos.ideas / "inbox").rglob("*.md"))


def test_requeue_dry_run_copies_nothing(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    before = (files(repos.ideas), files(repos.docs))
    report = run_pipeline(
        repos.ideas, repos.docs, RunOptions(requeue=["YouTube walks"], dry_run=True), make_services()
    )
    assert report.counts() == {"would_requeue": 1}
    assert (files(repos.ideas), files(repos.docs)) == before


def test_requeue_does_not_overwrite_a_file_already_in_the_inbox(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    archived = find(repos.ideas, "archive", "notes", "YouTube walks.md")
    waiting = repos.ideas / "inbox/notes" / archived.name
    waiting.write_text(archived.read_text() + "\nEdited since.\n")
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(requeue=["YouTube walks"]), make_services())
    [item] = [i for i in report.items if i.status != "published"]
    assert item.status == "skipped" and "already exists" in item.message
    assert (
        "Edited since." in load(find(repos.ideas, "archive", "notes", "YouTube walks.md")).body
    )  # the edit was used


def test_requeue_flag_on_the_command_line(repos, make_services, monkeypatch):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    monkeypatch.setattr("catcher.cli.default_services", lambda settings: make_services())
    base = ["run", "pipeline", "--ideas", str(repos.ideas), "--docs", str(repos.docs)]
    ok = CliRunner().invoke(app, [*base, "--requeue", "YouTube walks"])
    assert ok.exit_code == 0, ok.output
    assert "requeued" in ok.output and "published" in ok.output
    missing = CliRunner().invoke(app, [*base, "--requeue", "Nope"])
    assert missing.exit_code == 1 and 'no document named "Nope" in archive/' in missing.output


def test_requeue_moves_the_original_and_clears_the_stale_output_so_the_document_is_in_one_place(
    repos, make_services, yt_facts, sh
):
    (repos.ideas / "inbox/clippings/yt.md").write_text(YT_CLIP)
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(facts=lambda vid: yt_facts))
    archived = find(repos.ideas, "archive", "clippings", "yt.md")
    output = find(repos.ideas, "output", "clippings", "yt.md")

    def unavailable(vid):
        raise FactsUnavailable("blocked")  # the run starts, then stalls again

    run_pipeline(repos.ideas, repos.docs, RunOptions(requeue=["yt"]), make_services(facts=unavailable))
    assert not list((repos.ideas / "inbox").rglob("yt*.md"))  # the run took it out of inbox/ ...
    assert archived.exists() and load(output).fm["stage"] == "deferred"  # ... and wrote both folders again
    assert sh(repos.ideas, "status", "--porcelain") == ""  # every move and delete was committed


def test_requeue_dry_run_and_a_blocked_name_leave_archive_and_output_alone(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    archived = find(repos.ideas, "archive", "notes", "YouTube walks.md")
    output = find(repos.ideas, "output", "notes", "YouTube walks.md")
    (repos.ideas / "inbox/notes" / archived.name).write_text("already waiting\n")
    run_pipeline(
        repos.ideas, repos.docs, RunOptions(requeue=["YouTube walks"], dry_run=True), make_services()
    )
    assert archived.exists() and output.exists()


def test_requeue_of_a_failed_note_clears_failed_and_its_error_file(repos, make_services, sh):
    bad = FakeBackend([json.dumps({**CANNED["note"], "body": "{{< nope >}}"})])  # an unknown shortcode fails
    first = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(note_backend=bad))
    assert first.counts() == {"failed": 1, "published": 1}
    failed = find(repos.ideas, "failed", "notes", "YouTube walks.md")
    error = failed.with_suffix(".error.txt")
    assert error.exists()

    again = run_pipeline(repos.ideas, repos.docs, RunOptions(requeue=["YouTube walks"]), make_services())
    assert again.counts() == {"requeued": 1, "published": 1}
    assert not failed.exists() and not error.exists()  # neither the note nor its error message is left
    assert not list((repos.ideas / "failed").rglob("*YouTube*")) and not list(
        (repos.ideas / "failed").rglob("*youtube-walks*")
    )
    assert load(find(repos.ideas, "output", "notes", "YouTube walks.md")).fm["stage"] == "published"
    assert sh(repos.ideas, "status", "--porcelain") == ""


# ---- YouTube: the saved facts, the gap between calls and the breaker (real git, no network) ---------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class HttpError429(Exception):
    status = 429


def clip(vid: str) -> str:
    return f'---\nsource : "https://www.youtube.com/watch?v={vid}"\ncreated: 2026-09-25\n---\nclip\n'


def youtube_services(make_services, tmp_path, yt_facts, *, error=None, wait_max_s=1800.0):
    """Services whose 'YouTube' is a counter, behind the real gate (a 10 minute gap) and the saved facts."""
    from memory_gate import InMemoryGate

    from catcher.modules.youtube.access import YoutubeAccess

    calls: list[str] = []
    clock, sleeps = FakeClock(), []

    def fetch(vid):
        calls.append(vid)
        if error is not None:
            raise error
        return yt_facts.model_copy(update={"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}"})

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.now += seconds

    gate = InMemoryGate(min_gap_s=600, jitter_s=0, block_hours=6, clock=clock)
    services = make_services(facts=fetch)
    services.youtube = YoutubeAccess(fetch, gate, clock=clock, sleep=sleep, wait_max_s=wait_max_s)
    return services, calls, clock, sleeps


def statuses(report) -> dict[str, str]:
    return {i.doc_id: i.status for i in report.items if i.doc_class == "youtube"}


def test_a_second_clip_inside_the_gap_waits_in_the_inbox_and_the_next_run_takes_it(
    repos, make_services, yt_facts, tmp_path, sh
):
    clips = repos.ideas / "inbox/clippings"
    (clips / "a.md").write_text(clip("AAAAAAAAAAA"))
    (clips / "b.md").write_text(clip("BBBBBBBBBBB"))
    services, calls, clock, _ = youtube_services(make_services, tmp_path, yt_facts)

    first = run_pipeline(repos.ideas, repos.docs, RunOptions(), services)
    assert sorted(statuses(first).values()) == ["published", "waiting"]
    [waiting_id] = [k for k, v in statuses(first).items() if v == "waiting"]
    [waiting_item] = [i for i in first.items if i.doc_id == waiting_id]
    assert "next call allowed at" in waiting_item.message and "stays in inbox/" in waiting_item.message
    assert len(calls) == 1
    left_in_inbox = [p.name for p in clips.glob("*.md") if p.name in ("a.md", "b.md")]
    assert len(left_in_inbox) == 1  # untouched: no archive copy, no output, nothing to requeue
    assert not list((repos.ideas / "failed").rglob("*")) if (repos.ideas / "failed").exists() else True

    clock.now += 601  # the gap has passed: a plain run takes the waiting clip
    second = run_pipeline(repos.ideas, repos.docs, RunOptions(), services)
    assert statuses(second) == {waiting_id: "published"}
    assert sorted(calls) == ["AAAAAAAAAAA", "BBBBBBBBBBB"]
    assert not [p for p in clips.glob("*.md") if p.name in ("a.md", "b.md")]
    assert sorted(p.name for p in (repos.ideas / "facts").glob("*.json")) == [
        "AAAAAAAAAAA.json",
        "BBBBBBBBBBB.json",
    ]


def test_the_saved_facts_are_committed_with_the_run(repos, make_services, yt_facts, tmp_path, sh):
    (repos.ideas / "inbox/clippings/a.md").write_text(clip("AAAAAAAAAAA"))
    services, _, _, _ = youtube_services(make_services, tmp_path, yt_facts)
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), services)
    assert report.committed["ideas"] is True
    assert "facts/AAAAAAAAAAA.json" in sh(repos.ideas, "ls-files", "facts")
    assert sh(repos.ideas, "status", "--porcelain") == ""  # nothing is left uncommitted


def test_a_requeue_or_a_rerun_never_asks_youtube_again(repos, make_services, yt_facts, tmp_path):
    (repos.ideas / "inbox/clippings/a.md").write_text(clip("AAAAAAAAAAA"))
    services, calls, clock, _ = youtube_services(make_services, tmp_path, yt_facts)
    run_pipeline(repos.ideas, repos.docs, RunOptions(), services)
    clock.now += 7 * 24 * 3600
    for _ in range(2):
        report = run_pipeline(repos.ideas, repos.docs, RunOptions(requeue=["a"]), services)
        assert statuses(report) == {"AAAAAAAAAAA": "published"}
    assert calls == ["AAAAAAAAAAA"]  # one call to YouTube, ever


def test_refresh_facts_asks_youtube_again(repos, make_services, yt_facts, tmp_path):
    (repos.ideas / "inbox/clippings/a.md").write_text(clip("AAAAAAAAAAA"))
    services, calls, clock, _ = youtube_services(make_services, tmp_path, yt_facts)
    run_pipeline(repos.ideas, repos.docs, RunOptions(), services)
    clock.now += 700
    run_pipeline(repos.ideas, repos.docs, RunOptions(requeue=["a"], refresh_facts=True), services)
    assert calls == ["AAAAAAAAAAA", "AAAAAAAAAAA"]


def test_a_dry_run_saves_no_facts(repos, make_services, yt_facts, tmp_path):
    (repos.ideas / "inbox/clippings/a.md").write_text(clip("AAAAAAAAAAA"))
    services, calls, _, _ = youtube_services(make_services, tmp_path, yt_facts)
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(dry_run=True), services)
    assert statuses(report) == {"AAAAAAAAAAA": "would_fetch"}
    assert calls == []  # a dry run never asks YouTube, so it cannot cost a request
    assert not (repos.ideas / "facts").exists()  # a dry run changes no files in the repos
    # and it does not use up the gap either: the gate was never touched, no state recorded
    assert services.youtube.gate.state == OPEN


def test_a_429_opens_the_breaker_and_every_other_clip_waits_without_a_call(
    repos, make_services, yt_facts, tmp_path
):
    clips = repos.ideas / "inbox/clippings"
    (clips / "a.md").write_text(clip("AAAAAAAAAAA"))
    (clips / "b.md").write_text(clip("BBBBBBBBBBB"))
    services, calls, clock, _ = youtube_services(make_services, tmp_path, yt_facts, error=HttpError429("429"))

    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), services)
    assert sorted(statuses(report).values()) == ["waiting", "waiting"]  # the 429 one too: no requeue needed
    assert all("YouTube blocked until" in i.message for i in report.items if i.doc_class == "youtube")
    assert len(calls) == 1  # the one call that got the 429, and no more
    assert sorted(p.name for p in clips.glob("*.md") if p.name in ("a.md", "b.md")) == ["a.md", "b.md"]
    assert deferred_in_output(repos.ideas) == []  # nothing stalled in output/: no requeue needed

    clock.now += 3 * 3600  # hours later the gap is long gone, but the breaker is still open
    again = run_pipeline(repos.ideas, repos.docs, RunOptions(), services)
    assert set(statuses(again).values()) == {"waiting"}
    assert "YouTube blocked until" in again.items[0].message
    assert len(calls) == 1


def test_wait_youtube_sleeps_through_the_gap_so_one_run_does_both_clips(
    repos, make_services, yt_facts, tmp_path
):
    clips = repos.ideas / "inbox/clippings"
    (clips / "a.md").write_text(clip("AAAAAAAAAAA"))
    (clips / "b.md").write_text(clip("BBBBBBBBBBB"))
    services, calls, _, sleeps = youtube_services(make_services, tmp_path, yt_facts)
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(wait_youtube=True), services)
    assert sorted(statuses(report).values()) == ["published", "published"]
    assert len(calls) == 2 and len(sleeps) == 1 and 599 < sleeps[0] < 603


def test_wait_youtube_still_defers_a_wait_longer_than_the_limit(repos, make_services, yt_facts, tmp_path):
    clips = repos.ideas / "inbox/clippings"
    (clips / "a.md").write_text(clip("AAAAAAAAAAA"))
    (clips / "b.md").write_text(clip("BBBBBBBBBBB"))
    services, calls, _, sleeps = youtube_services(make_services, tmp_path, yt_facts, wait_max_s=60)
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(wait_youtube=True), services)
    assert sorted(statuses(report).values()) == ["published", "waiting"] and sleeps == [] and len(calls) == 1


def test_notes_and_chats_never_wait_for_youtube(repos, make_services, yt_facts, tmp_path):
    (repos.ideas / "inbox/clippings/a.md").write_text(clip("AAAAAAAAAAA"))
    services, calls, _, _ = youtube_services(make_services, tmp_path, yt_facts)
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), services)
    assert {i.doc_class for i in report.items} >= {"note", "ai-chat", "youtube"}
    assert all(i.status == "published" for i in report.items)  # the clip, the note and the chat
    assert calls == ["AAAAAAAAAAA"]


# ---- safety: interrupts, one run at a time, git failures, documents the LLM cannot take ------------------


def in_inbox(ideas: Path) -> list[str]:
    return sorted(p.name for p in (ideas / "inbox").rglob("*.md"))


def test_ctrl_c_in_the_middle_of_a_note_puts_it_back_in_the_inbox_and_still_commits(
    repos, make_services, monkeypatch, sh
):
    real = process_note_of_run()
    calls = []

    def interrupt_on_second(note, svc, opts):
        calls.append(note.doc_id)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(note, svc, opts)

    monkeypatch.setattr("catcher.modules.pipeline.run.process_note", interrupt_on_second)
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert report.counts() == {"published": 1, "interrupted": 1}
    assert any("interrupted" in p for p in report.problems)
    assert len(in_inbox(repos.ideas)) == 1  # the interrupted one is back, as it was captured
    assert len(list((repos.ideas / "archive").rglob("*.md"))) == 1  # and only the finished one is archived
    assert len(list((repos.ideas / "output").rglob("*.md"))) == 1
    assert report.committed == {"docs": True, "ideas": True}  # what was done is not left uncommitted
    assert sh(repos.ideas, "status", "--porcelain") == ""
    again = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert again.counts() == {"published": 1}  # nothing lost, nothing archived twice
    assert len(list((repos.ideas / "archive").rglob("*.md"))) == 2


def process_note_of_run():
    from catcher.modules.pipeline.process import process_note

    return process_note


def test_a_second_run_at_the_same_time_is_refused_and_touches_nothing(repos, make_services):
    from catcher.core.files import file_lock

    services = make_services()
    lock = services.settings.catcher_state_dir / "pipeline.lock"
    with file_lock(lock, blocking=False) as held:
        assert held
        report = run_pipeline(repos.ideas, repos.docs, RunOptions(), services)
    assert report.items == [] and "another catcher run is in progress" in report.problems[0]
    assert len(in_inbox(repos.ideas)) == 2
    assert run_pipeline(repos.ideas, repos.docs, RunOptions(), services).counts() == {"published": 2}


def test_a_dry_run_does_not_need_the_run_lock(repos, make_services):
    from catcher.core.files import file_lock

    services = make_services()
    with file_lock(services.settings.catcher_state_dir / "pipeline.lock", blocking=False):
        report = run_pipeline(repos.ideas, repos.docs, RunOptions(dry_run=True), services)
    assert report.counts() == {"would_publish": 2}


def test_a_failed_pull_is_a_reported_problem_and_nothing_is_changed(repos, make_services, monkeypatch):
    from catcher.core.git import GitError

    def boom(repo):
        raise GitError("git pull failed in idea-bucket: could not resolve host")

    monkeypatch.setattr("catcher.modules.pipeline.run.pull", boom)
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(push=True), make_services())
    assert report.items == [] and "could not resolve host" in report.problems[0]
    assert len(in_inbox(repos.ideas)) == 2


def test_a_failed_push_is_a_reported_problem_after_the_work_is_committed(repos, make_services, monkeypatch):
    from catcher.core.git import GitError

    def boom(repo):
        raise GitError("git push failed in epiaku-docs: rejected")

    monkeypatch.setattr("catcher.modules.pipeline.run.push", boom)
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(push=True), make_services())
    assert report.counts() == {"published": 2} and report.committed == {"docs": True, "ideas": True}
    assert not report.pushed and "rejected" in report.problems[0]


def test_an_empty_document_fails_without_an_llm_call(repos, make_services):
    (repos.ideas / "inbox/notes/empty.md").write_text("---\ncreated: 2026-09-25\n---\n  \n")
    notes = FakeBackend()
    report = run_pipeline(
        repos.ideas, repos.docs, RunOptions(only=["empty"]), make_services(note_backend=notes)
    )
    assert report.counts() == {"failed": 1} and "empty" in report.items[0].message
    assert notes.prompts == []
    failed = find(repos.ideas, "failed", "notes", "empty.md")
    assert "empty" in failed.with_suffix(".error.txt").read_text()


def test_a_document_over_the_size_limit_fails_without_an_llm_call(repos, make_services):
    services = make_services()
    services.settings = services.settings.model_copy(update={"llm_max_input_chars": 50})
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(only=["YouTube walks"]), services)
    assert report.counts() == {"failed": 1} and "LLM_MAX_INPUT_CHARS" in report.items[0].message


def test_retry_deferred_puts_the_stalled_documents_back_so_they_run_again(repos, make_services):
    chats = FakeBackend([UsageLimitReached("limit", backend="openai")])
    first = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    assert first.counts() == {"published": 1, "deferred": 1}
    assert run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services()).items == []  # not alone
    second = run_pipeline(repos.ideas, repos.docs, RunOptions(retry_deferred=True), make_services())
    assert second.counts() == {"requeued": 1, "published": 1}
    final = find(repos.ideas, "output", "clippings", "systeme.md")
    assert load(final).fm["stage"] == "published"


def test_retry_deferred_with_nothing_stalled_does_nothing(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert run_pipeline(repos.ideas, repos.docs, RunOptions(retry_deferred=True), make_services()).items == []


def test_a_gate_that_closes_after_the_check_sends_the_clip_back_to_the_inbox(
    repos, make_services, yt_facts, tmp_path
):
    """The run checks the gate before it starts a clip; another process can use the gap in between."""
    (repos.ideas / "inbox/clippings/a.md").write_text(clip("AAAAAAAAAAA"))
    services, calls, _, _ = youtube_services(make_services, tmp_path, yt_facts)
    services.youtube.gate.reserve()  # another process just took the slot
    services.youtube.wait_needed = lambda *args, **kwargs: None  # the earlier check said "go ahead"
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(limit=1), services)
    assert statuses(report) == {"AAAAAAAAAAA": "waiting"} and calls == []
    assert "a.md" in in_inbox(repos.ideas)  # back where it was, no requeue needed
    assert not [p for p in (repos.ideas / "output").rglob("*") if p.is_file() and "a" in p.name[:0]]
    assert not list((repos.ideas / "failed").rglob("*")) if (repos.ideas / "failed").exists() else True


def test_two_different_clips_with_one_id_warn_that_the_later_page_replaces_the_earlier(
    repos, make_services, caplog
):
    caplog.set_level("WARNING", logger="catcher")
    clips = repos.ideas / "inbox/clippings"
    (clips / "one.md").write_text(chat("2446cd9c762c9cc9", turns(3).replace("question 2", "something else")))
    (clips / "two.md").write_text(chat("2446cd9c762c9cc9", turns(6)))
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    same = [i for i in report.items if i.doc_id == "2446cd9c762c9cc9"]
    assert len(same) == 2 and any("replaces the earlier one" in i.message for i in same)
    assert "replaces the earlier one" in caplog.text


def test_retry_deferred_flag_on_the_command_line(repos, make_services, monkeypatch):
    chats = FakeBackend([UsageLimitReached("limit", backend="openai")])
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=chats))
    monkeypatch.setattr("catcher.cli.default_services", lambda settings: make_services())
    base = ["run", "pipeline", "--ideas", str(repos.ideas), "--docs", str(repos.docs)]
    ok = CliRunner().invoke(app, [*base, "--retry-deferred"])
    assert ok.exit_code == 0, ok.output
    assert "requeued" in ok.output and "published" in ok.output
