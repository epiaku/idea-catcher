"""`ItemReport.llm_saved`: the page came from a saved LLM reply (tokens recorded, not spent)."""

from types import SimpleNamespace

import pytest
from run_on_worker import run_on_worker
from sqlalchemy import Engine

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _database(pg_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_on_worker` runs the command's path (B5b) on the test database."""
    monkeypatch.setenv("DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))


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
    first = run_on_worker(repos, make_services())
    assert len(published(first)) == 2
    assert [i.llm_saved for i in published(first)] == [False, False]

    requeue = {"requeue": ["systeme", "YouTube walks"]}
    again = run_on_worker(repos, make_services(), **requeue)
    assert len(published(again)) == 2
    assert [i.llm_saved for i in published(again)] == [True, True]
    assert all(i.tokens_in for i in published(again))  # the recorded counts stay on the item

    refresh = {"requeue": ["systeme", "YouTube walks"], "refresh_llm": True}
    fresh = run_on_worker(repos, make_services(), **refresh)
    assert [i.llm_saved for i in published(fresh)] == [False, False]
