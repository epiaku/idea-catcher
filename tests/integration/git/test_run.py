import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from catcher.cli import app
from catcher.core.frontmatter import load
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.llm.service import BudgetExhausted, UsageLimitReached
from catcher.modules.pipeline.inbox import slugify_title
from catcher.modules.pipeline.run import RunOptions, run_pipeline

REPO = Path(__file__).parents[3]
NOTES = "hugo/content/en/docs/idea-bucket/notes"
CLIPPING = "hugo/content/en/docs/idea-bucket/clippings"


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
    assert note_page.endswith("-youtube-walks.md")
    [chat_page] = [p.name for p in (repos.docs / CLIPPING).glob("2026*.md")]
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
    assert (repos.docs / CLIPPING / final.name).read_text().startswith("---\n")  # ... and in epiaku-docs
    assert load(repos.docs / CLIPPING / final.name).fm["id"] == "cf81e40b020519ef"
    assert load(final).fm["source_file"] == f"clippings/{final.name}"
    assert load(final).fm["original_filename"] == "systeme.md"
    assert "stage" not in load(final).fm
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
    pages = sorted((repos.docs / CLIPPING).glob("2026*.md"))
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
    assert final.with_suffix(".youtube.json").exists()
    assert "stage" not in load(final).fm
    assert find(repos.ideas, "archive", "clippings", "yt.md").name == final.name  # not a second archive copy
    assert (repos.docs / "hugo/content/en/docs/idea-bucket/youtube" / final.name).exists()


def test_run_logs_progress_per_note_and_a_total(repos, make_services, caplog):
    caplog.set_level("INFO", logger="catcher")
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    messages = [r.getMessage() for r in caplog.records]
    assert any("(1/2)" in m and "processing" in m for m in messages)
    assert any("(2/2)" in m and "published" in m for m in messages)
    assert any(m.startswith("processed 2/2: 2 published") for m in messages)
    assert any(m.startswith("archived archive/notes/") and m.endswith("-youtube-walks.md") for m in messages)
    assert any(m.startswith("named YouTube walks.md ->") for m in messages)
    assert any('"inbox/notes/YouTube walks.md"' in m and "published" in m for m in messages)
    assert any('"inbox/clippings/systeme.md"' in m and "processing" in m for m in messages)


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
    assert final.name == stalled.name and "stage" not in load(final).fm


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
    assert "stage" not in load(find(repos.ideas, "output", "clippings", "chat long.md")).fm
    pages = [p for p in (repos.docs / CLIPPING).glob("2026*.md") if load(p).fm["id"] == "2446cd9c762c9cc9"]
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
