"""A run reads the saved LLM reply in `llm/` before it calls the model, like it reads the saved YouTube facts.

Fresh output only with `--refresh-llm`, `LLM_CACHE=false` or by deleting the trace file: nothing implicit.
"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from catcher import cli
from catcher.core.config import Settings
from catcher.core.frontmatter import load
from catcher.modules.llm import service
from catcher.modules.llm.backends.fake import CANNED, FakeBackend
from catcher.modules.pipeline.run import RunOptions, RunReport, run_pipeline
from catcher.modules.pipeline.tags import TagList, load_tags

NOTES = "hugo/content/en/docs/idea-bucket/notes"
CLIPPING = "hugo/content/en/docs/idea-bucket/clippings"
NOTE_BODY = "Create YouTube content walking around\n"
SAVED = "using the saved LLM reply (no call)"


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
            "inbox/notes/YouTube walks.md": NOTE_BODY,
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


def page_of(repos: SimpleNamespace, sub: str, original: str) -> Path:
    folder = NOTES if sub == "notes" else CLIPPING
    return repos.docs / folder / archived(repos.ideas, sub, original).name


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def traces(ideas: Path) -> list[Path]:
    return sorted((ideas / "llm").rglob("*.json")) if (ideas / "llm").is_dir() else []


def never_build(profile):
    raise AssertionError(f"the backend must not be built: a saved reply exists ({profile.backend})")


def no_backend(make_services):
    services = make_services()
    services.backends = never_build
    return services


DOCS = (("notes", "YouTube walks.md"), ("clippings", "systeme.md"))


def without_id(page: str) -> str:
    """A note's frontmatter `id` is drawn again on each scan (Stage A), so a requeued note has a new one."""
    return "\n".join(line for line in page.splitlines() if not line.startswith("id: "))


def test_a_second_run_reuses_the_saved_reply_and_never_calls_the_backend(repos, make_services, sh, caplog):
    first = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    live = {doc: page_of(repos, *doc).read_text() for doc in DOCS}
    saved = {p: p.read_bytes() for p in traces(repos.ideas)}
    assert len(saved) == 2

    caplog.set_level(logging.INFO, logger="catcher")
    requeue = RunOptions(requeue=["systeme", "YouTube walks"])
    report = run_pipeline(repos.ideas, repos.docs, requeue, no_backend(make_services))
    assert report.counts() == {"requeued": 2, "published": 2}
    assert {p: p.read_bytes() for p in traces(repos.ideas)} == saved  # untouched, no new file
    hits = [r for r in caplog.records if r.name == "catcher.process" and SAVED in r.getMessage()]
    assert len(hits) == 2
    tokens = {i.doc_class: (i.tokens_in, i.tokens_out) for i in first.items}
    assert {i.doc_class: (i.tokens_in, i.tokens_out) for i in report.items if i.page} == tokens  # as recorded

    assert page_of(repos, *DOCS[1]).read_text() == live[DOCS[1]]  # byte-identical
    assert without_id(page_of(repos, *DOCS[0]).read_text()) == without_id(live[DOCS[0]])

    again = run_pipeline(repos.ideas, repos.docs, requeue, no_backend(make_services))
    assert again.counts() == {"requeued": 2, "published": 2}
    assert page_of(repos, *DOCS[1]).read_text() == live[DOCS[1]]
    assert {p: p.read_bytes() for p in traces(repos.ideas)} == saved
    assert sh(repos.ideas, "status", "--porcelain") == ""
    assert sh(repos.docs, "status", "--porcelain") == ""


def test_a_page_from_a_saved_reply_is_byte_identical_to_the_live_page(repos, make_services):
    # a real-looking reply: the provider answers with a dated model name, not the profile's model
    class DatedModel(FakeBackend):
        def complete(self, prompt, *, model, task):
            reply = super().complete(prompt, model=model, task=task)
            reply.model = "gpt-test-2026-09-01"
            return reply

    run_pipeline(
        repos.ideas, repos.docs, RunOptions(only=["systeme"]), make_services(chat_backend=DatedModel())
    )
    page = page_of(repos, "clippings", "systeme.md")
    live = page.read_text()
    assert "gpt-test-2026-09-01" in live

    report = run_pipeline(repos.ideas, repos.docs, RunOptions(requeue=["systeme"]), no_backend(make_services))
    assert report.counts() == {"requeued": 1, "published": 1}
    assert page.read_text() == live  # nothing dropped: backend, model and prompt version are as recorded


def v2_chat() -> FakeBackend:
    return FakeBackend([json.dumps({**CANNED["ai-chat"], "title": "Bundles v2"})])


def test_requeue_alone_reuses_the_saved_reply(repos, make_services, sh):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    path = trace_of(repos.ideas, "clippings", "systeme.md")
    first = path.read_bytes()

    chats = v2_chat()
    report = run_pipeline(
        repos.ideas, repos.docs, RunOptions(requeue=["systeme"]), make_services(chat_backend=chats)
    )
    assert report.counts() == {"requeued": 1, "published": 1}
    assert chats.prompts == []  # nothing implicit: a requeue alone does not call the model
    assert path.read_bytes() == first
    assert load(page_of(repos, "clippings", "systeme.md")).fm["title"] == "Fake Chat"
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_refresh_llm_calls_the_model_again_and_overwrites_the_trace(repos, make_services, sh):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    path = trace_of(repos.ideas, "clippings", "systeme.md")
    first = path.read_bytes()

    chats = v2_chat()
    opts = RunOptions(requeue=["systeme"], refresh_llm=True)
    report = run_pipeline(repos.ideas, repos.docs, opts, make_services(chat_backend=chats))
    assert report.counts() == {"requeued": 1, "published": 1}
    assert len(chats.prompts) == 1
    assert path.read_bytes() != first and read(path)["output"]["title"] == "Bundles v2"
    assert read(path)["backend"] == "fake" and read(path)["attempts"][0]["tokens_in"]
    assert len(traces(repos.ideas)) == 2
    page = load(page_of(repos, "clippings", "systeme.md"))
    assert page.fm["title"] == "Bundles v2" and page.fm["llm"]["backend"] == "fake"
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_requeue_with_refresh_llm_calls_the_model_again(repos, make_services, monkeypatch):
    # The flags reach the run: `--requeue` leaves `refresh_llm` off, `--refresh-llm` turns it on
    seen: list[RunOptions] = []

    def fake_run(ideas, docs, opts, svc):
        seen.append(opts)
        return RunReport()

    monkeypatch.setattr(cli, "run_pipeline", fake_run)
    monkeypatch.setattr(cli, "default_services", lambda settings: None)
    runner = CliRunner()
    args = ["run", "pipeline", "--ideas", str(repos.ideas), "--docs", str(repos.docs), "--requeue", "systeme"]
    assert runner.invoke(cli.app, args).exit_code == 0
    assert runner.invoke(cli.app, [*args, "--refresh-llm"]).exit_code == 0
    assert [(o.requeue, o.refresh_llm) for o in seen] == [(["systeme"], False), (["systeme"], True)]
    assert "--refresh-llm" in runner.invoke(cli.app, ["run", "pipeline", "--help"]).output

    # ... and the run then calls the model for the requeued document, not for the others
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    chats, notes = v2_chat(), FakeBackend()
    opts = RunOptions(requeue=["systeme"], refresh_llm=True)
    report = run_pipeline(
        repos.ideas, repos.docs, opts, make_services(note_backend=notes, chat_backend=chats)
    )
    assert report.counts() == {"requeued": 1, "published": 1}
    assert len(chats.prompts) == 1 and notes.prompts == []


def test_deleting_the_trace_file_gives_a_fresh_call(repos, make_services, sh):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    path = trace_of(repos.ideas, "clippings", "systeme.md")
    path.unlink()

    chats = v2_chat()
    report = run_pipeline(
        repos.ideas, repos.docs, RunOptions(requeue=["systeme"]), make_services(chat_backend=chats)
    )
    assert report.counts() == {"requeued": 1, "published": 1}
    assert len(chats.prompts) == 1 and read(path)["output"]["title"] == "Bundles v2"
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_a_prompt_version_change_is_a_miss(repos, make_services, monkeypatch):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    path = trace_of(repos.ideas, "clippings", "systeme.md")
    old_version = read(path)["prompt_version"]

    # `reason()` takes the version from `render_prompt` (the prompt file's frontmatter): bump it there
    real_render = service.render_prompt
    monkeypatch.setattr(service, "render_prompt", lambda task, v: (real_render(task, v)[0], "bumped-99"))
    chats = v2_chat()
    report = run_pipeline(
        repos.ideas, repos.docs, RunOptions(requeue=["systeme"]), make_services(chat_backend=chats)
    )
    assert report.counts() == {"requeued": 1, "published": 1}
    assert len(chats.prompts) == 1
    assert old_version != "bumped-99" and read(path)["prompt_version"] == "bumped-99"
    assert load(page_of(repos, "clippings", "systeme.md")).fm["llm"]["prompt_version"] == "bumped-99"


def test_another_profile_is_a_miss(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(only=["systeme"]), make_services())
    path = trace_of(repos.ideas, "clippings", "systeme.md")
    assert read(path)["profile"] == "clippings"

    notes = FakeBackend([json.dumps({**CANNED["ai-chat"], "title": "Via fake"})])
    opts = RunOptions(requeue=["systeme"], profile="fake")
    report = run_pipeline(repos.ideas, repos.docs, opts, make_services(note_backend=notes))
    assert report.counts() == {"requeued": 1, "published": 1}
    assert len(notes.prompts) == 1
    assert read(path)["profile"] == "fake" and read(path)["output"]["title"] == "Via fake"


def test_an_invalid_output_trace_is_not_reused(repos, make_services, sh):
    bad = FakeBackend(["nope", "still nope"])
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(chat_backend=bad))
    assert report.counts() == {"published": 1, "failed": 1}
    path = trace_of(repos.ideas, "clippings", "systeme.md")
    assert read(path)["outcome"] == "invalid_output"

    chats = FakeBackend()
    report = run_pipeline(
        repos.ideas, repos.docs, RunOptions(requeue=["systeme"]), make_services(chat_backend=chats)
    )
    assert report.counts() == {"requeued": 1, "published": 1}
    assert len(chats.prompts) == 1 and read(path)["outcome"] == "ok"
    assert page_of(repos, "clippings", "systeme.md").exists()
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_llm_cache_false_always_calls(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    path = trace_of(repos.ideas, "clippings", "systeme.md")

    chats = v2_chat()
    services = make_services(chat_backend=chats)
    services.settings = Settings(llm_cache=False)
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(requeue=["systeme"]), services)
    assert report.counts() == {"requeued": 1, "published": 1}
    assert len(chats.prompts) == 1 and read(path)["output"]["title"] == "Bundles v2"  # still written


def test_a_dry_run_reads_saved_replies_and_writes_nothing(repos, make_services, sh, caplog):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    saved = {p: p.read_bytes() for p in traces(repos.ideas)}
    # the same text captured again under another name: its text matches a saved reply
    (repos.ideas / "inbox/notes/YouTube walks again.md").write_text(NOTE_BODY)

    caplog.set_level(logging.INFO, logger="catcher")
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(dry_run=True), no_backend(make_services))
    assert report.counts() == {"would_publish": 1}
    assert any(SAVED in r.getMessage() for r in caplog.records if r.name == "catcher.process")
    assert {p: p.read_bytes() for p in traces(repos.ideas)} == saved
    status = sh(repos.ideas, "status", "--porcelain", "-uall").splitlines()
    assert len(status) == 1 and status[0].startswith("??") and "YouTube walks again.md" in status[0]
    assert sh(repos.docs, "status", "--porcelain") == ""


def test_the_saved_reply_is_validated_again(repos, make_services, sh, caplog):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    path = trace_of(repos.ideas, "clippings", "systeme.md")
    edited = read(path)
    edited["attempts"][0]["reply"] = '{"title": "hand-edited and no longer valid"}'
    assert edited["outcome"] == "ok"
    path.write_text(json.dumps(edited, indent=2), encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="catcher")
    chats = v2_chat()
    report = run_pipeline(
        repos.ideas, repos.docs, RunOptions(requeue=["systeme"]), make_services(chat_backend=chats)
    )
    assert report.counts() == {"requeued": 1, "published": 1}
    assert len(chats.prompts) == 1  # the saved reply failed validation: the model is called instead
    warnings = [r.getMessage() for r in caplog.records if r.name == "catcher.llm"]
    assert len(warnings) == 1 and warnings[0].startswith("ai-chat: ")
    assert read(path)["output"]["title"] == "Bundles v2"  # the real call is recorded as usual
    assert load(page_of(repos, "clippings", "systeme.md")).fm["title"] == "Bundles v2"
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_a_new_tag_order_applies_to_a_saved_reply(repos, make_services):
    reply = json.dumps({**CANNED["note"], "tags": ["todo", "app-idea", "automation"]})
    run_pipeline(
        repos.ideas, repos.docs, RunOptions(only=["YouTube walks"]), make_services(FakeBackend([reply]))
    )
    page = page_of(repos, "notes", "YouTube walks.md")
    assert load(page).fm["tags"][0] == "app-idea"  # app-idea ranks above todo today

    tags = load_tags()
    reordered = TagList(("todo", *(t for t in tags.idea_types if t != "todo")), tags.topics, tags.projects)
    services = no_backend(make_services)
    services.tags = reordered
    report = run_pipeline(repos.ideas, repos.docs, RunOptions(requeue=["YouTube walks"]), services)
    assert report.counts() == {"requeued": 1, "published": 1}
    assert load(page).fm["tags"][0] == "todo"  # the saved reply, the new order
    assert "app-idea" not in load(page).fm["tags"]


BAD_PAGE = json.dumps({**CANNED["note"], "body": "{{< nope >}}"})  # valid reply, but an unknown shortcode


def test_a_reply_that_made_an_invalid_page_is_not_reused(repos, make_services, sh):
    first = run_pipeline(
        repos.ideas, repos.docs, RunOptions(), make_services(note_backend=FakeBackend([BAD_PAGE]))
    )
    assert first.counts() == {"failed": 1, "published": 1}
    path = trace_of(repos.ideas, "notes", "YouTube walks.md")
    trace = read(path)
    assert trace["outcome"] == "invalid_page" and trace["error"].startswith("page is invalid: ")
    assert trace["attempts"][0]["reply"] == BAD_PAGE and trace["output"]["body"] == "{{< nope >}}"
    assert sh(repos.ideas, "status", "--porcelain") == ""  # the marked trace is committed

    notes = FakeBackend()
    again = run_pipeline(
        repos.ideas, repos.docs, RunOptions(requeue=["YouTube walks"]), make_services(note_backend=notes)
    )
    assert again.counts() == {"requeued": 1, "published": 1}
    assert len(notes.prompts) == 1  # a plain requeue asks the model again
    assert read(path)["outcome"] == "ok" and read(path)["output"]["body"] == CANNED["note"]["body"]
    assert sh(repos.ideas, "status", "--porcelain") == ""


def test_a_plain_requeue_after_an_invalid_page_tries_the_model(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services(note_backend=FakeBackend([BAD_PAGE])))
    report = run_pipeline(
        repos.ideas, repos.docs, RunOptions(requeue=["YouTube walks"]), no_backend(make_services)
    )
    failed = [i for i in report.items if i.status == "failed"]
    assert len(failed) == 1 and "the backend must not be built" in failed[0].message  # a call was attempted


def test_a_dry_run_marks_no_trace(repos, make_services):
    run_pipeline(repos.ideas, repos.docs, RunOptions(only=["YouTube walks"]), make_services())
    path = trace_of(repos.ideas, "notes", "YouTube walks.md")
    edited = read(path)
    edited["attempts"][0]["reply"] = BAD_PAGE  # still a valid reply, so still reused, but the page is invalid
    path.write_text(json.dumps(edited, indent=2), encoding="utf-8")
    before = path.read_bytes()
    (repos.ideas / "inbox/notes/YouTube walks again.md").write_text(NOTE_BODY)

    opts = RunOptions(dry_run=True, only=["YouTube walks again"])
    report = run_pipeline(repos.ideas, repos.docs, opts, no_backend(make_services))
    (item,) = report.items
    assert item.status == "failed" and "unknown shortcodes" in item.message  # the saved reply, no call
    assert path.read_bytes() == before and read(path)["outcome"] == "ok"
