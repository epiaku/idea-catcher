"""`ItemReport.llm_saved`: the page came from a saved LLM reply (tokens recorded, not spent)."""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from catcher import cli
from catcher.modules.pipeline.run import RunOptions, run_pipeline

NOTES = "hugo/content/en/docs/idea-bucket/notes"
WEB_CLIPS = "hugo/content/en/docs/idea-bucket/web-clips"
CHAT = (
    '---\nsource : "https://gemini.google.com/app/cf81e40b020519ef?is_sa=1"\n'
    'created: 2026-09-25\ntags:\n  - "clippings"\n---\n'
    "**You**\n\nsell bundles?\n\n---\n\n**Gemini**\n\nYes.\n"
)
MARK = "(saved reply)"


@pytest.fixture
def repos(make_repo):
    _, ideas = make_repo(
        "idea-bucket",
        {
            "inbox/notes/YouTube walks.md": "Create YouTube content walking around\n",
            "inbox/clippings/systeme.md": CHAT,
        },
    )
    _, docs = make_repo(
        "epiaku-docs",
        {
            f"{NOTES}/_index.md": "---\ntitle: Notes\n---\n",
            f"{WEB_CLIPS}/_index.md": "---\ntitle: Web clips\n---\n",
        },
    )
    return SimpleNamespace(ideas=ideas, docs=docs)


def published(report):
    return [i for i in report.items if i.status == "published"]


def test_the_flag_is_false_on_a_first_run_true_on_a_reused_reply_and_false_on_refresh(repos, make_services):
    first = run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    assert len(published(first)) == 2
    assert [i.llm_saved for i in published(first)] == [False, False]

    requeue = RunOptions(requeue=["systeme", "YouTube walks"])
    again = run_pipeline(repos.ideas, repos.docs, requeue, make_services())
    assert len(published(again)) == 2
    assert [i.llm_saved for i in published(again)] == [True, True]
    assert all(i.tokens_in for i in published(again))  # the recorded counts stay on the item

    refresh = RunOptions(requeue=["systeme", "YouTube walks"], refresh_llm=True)
    fresh = run_pipeline(repos.ideas, repos.docs, refresh, make_services())
    assert [i.llm_saved for i in published(fresh)] == [False, False]


def test_the_command_line_marks_only_the_item_served_from_a_saved_reply(repos, make_services, monkeypatch):
    run_pipeline(repos.ideas, repos.docs, RunOptions(), make_services())
    monkeypatch.setattr("catcher.cli.default_services", lambda settings: make_services())
    base = ["run", "pipeline", "--ideas", str(repos.ideas), "--docs", str(repos.docs)]
    runner = CliRunner()

    saved = runner.invoke(cli.app, [*base, "--requeue", "systeme"])
    assert saved.exit_code == 0, saved.output
    [line] = [x for x in saved.output.splitlines() if x.startswith("published")]
    assert line.endswith(MARK) and ".md (saved reply)" in line

    fresh = runner.invoke(cli.app, [*base, "--requeue", "YouTube walks", "--refresh-llm"])
    assert fresh.exit_code == 0, fresh.output
    [line] = [x for x in fresh.output.splitlines() if x.startswith("published")]
    assert MARK not in line
    assert not any(x.startswith("requeued") and MARK in x for x in saved.output.splitlines())
