"""The `pipeline.publish` handler: pull both repos, commit the managed folders, push right after each commit.

Only the harness's local bare remotes in tmp folders are used: no real remote, no network. A test on the
frozen harness (no remote) passes `push=False` or expects the clear "no remote" failure."""

from pathlib import Path

from worker_harness import NOTES

from catcher.core.db import session_scope
from catcher.modules.queue.models import Job
from catcher.modules.worker.app import build_handlers
from catcher.modules.worker.handlers import Done, Fail, HandlerResult
from catcher.modules.worker.handlers_pipeline import handle_pipeline_publish

IDEAS_MANAGED = ["inbox", "archive", "output", "failed", "duplicates", "facts", "llm"]


def publish(harness, **params) -> HandlerResult:
    job_id = harness.add_job("pipeline.publish", **params)
    with session_scope(harness.ctx.engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return handle_pipeline_publish(harness.ctx, job)


def bare(repo: Path) -> Path:
    """The harness's bare remote of a clone (make_repo puts `<name>.git` next to `<name>`)."""
    return repo.with_name(f"{repo.name}.git")


def remote_log(sh, repo: Path) -> list[str]:
    return sh(bare(repo), "log", "--format=%s", "main").splitlines()


def head_count(sh, repo: Path) -> int:
    return int(sh(repo, "rev-list", "--count", "HEAD").strip())


def test_pipeline_publish_is_registered():
    assert build_handlers()["pipeline.publish"] is handle_pipeline_publish


def test_publish_commits_both_repos_and_pushes_to_the_remotes(harness, sh):
    harness.add_job("pipeline.run", only=["YouTube walks"])
    assert harness.drain(max_jobs=3) == ["succeeded", "succeeded"]
    assert sh(harness.ideas, "status", "--porcelain") != ""

    result = publish(harness)

    assert result == Done({"committed": {"docs": True, "ideas": True}, "pushed": True})
    for repo in (harness.ideas, harness.docs):
        assert sh(repo, "status", "--porcelain") == ""
        assert head_count(sh, repo) == 2
        assert sh(repo, "rev-parse", "HEAD") == sh(bare(repo), "rev-parse", "main")  # pushed
        assert sh(repo, "log", "-1", "--format=%an <%ae>").strip() == (
            f"{harness.ctx.settings.git_author_name} <{harness.ctx.settings.git_author_email}>"
        )
    docs_files = sh(harness.docs, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert len(docs_files) == 1 and docs_files[0].startswith(f"{NOTES}/")
    ideas_files = sh(harness.ideas, "show", "--name-status", "--no-renames", "--format=", "HEAD").splitlines()
    assert "D\tinbox/notes/YouTube walks.md" in ideas_files
    assert all(line.split("\t")[1].split("/")[0] in IDEAS_MANAGED for line in ideas_files)


def test_a_failed_pull_fails_the_job_and_changes_nothing(harness, sh, tmp_path):
    (harness.ideas / "output/notes").mkdir(parents=True)
    (harness.ideas / "output/notes/x.md").write_text("x\n")
    sh(harness.docs, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    result = publish(harness)

    assert isinstance(result, Fail) and "git pull" in result.error
    assert head_count(sh, harness.ideas) == 1 and head_count(sh, harness.docs) == 1
    assert sh(harness.ideas, "status", "--porcelain").splitlines() == ["?? output/"]
    assert remote_log(sh, harness.ideas) == ["seed"]


def test_publish_twice_makes_no_second_commit(harness, sh):
    (harness.ideas / "facts").mkdir()
    (harness.ideas / "facts/v.json").write_text("{}\n")
    assert publish(harness) == Done({"committed": {"docs": False, "ideas": True}, "pushed": True})

    assert publish(harness) == Done({"committed": {"docs": False, "ideas": False}, "pushed": False})

    assert head_count(sh, harness.ideas) == 2 and head_count(sh, harness.docs) == 1
    assert remote_log(sh, harness.ideas) == ["idea-catcher: process the inbox (pipeline.publish)", "seed"]


def test_unrelated_docs_changes_are_not_committed(harness, sh):
    page = harness.docs / NOTES / "20261002_abcdef_x.md"
    page.write_text("page\n")
    artifact = harness.docs / "idea-bucket/artifacts/a.pdf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"%PDF\n")
    (harness.docs / "README.md").write_text("# edited by hand\n")
    (harness.docs / "hugo/content/en/docs/other.md").write_text("draft\n")

    assert publish(harness) == Done({"committed": {"docs": True, "ideas": False}, "pushed": True})

    assert sorted(sh(harness.docs, "show", "--name-only", "--format=", "HEAD").splitlines()) == [
        f"{NOTES}/20261002_abcdef_x.md",
        "idea-bucket/artifacts/a.pdf",
    ]
    assert sorted(sh(harness.docs, "status", "--porcelain").splitlines()) == [
        " M README.md",
        "?? hugo/content/en/docs/other.md",
    ]


def test_a_note_moved_out_of_inbox_is_committed_as_a_deletion(harness, sh):
    (harness.ideas / "archive/notes").mkdir(parents=True)
    (harness.ideas / "inbox/notes/YouTube walks.md").rename(harness.ideas / "archive/notes/YouTube walks.md")

    assert publish(harness) == Done({"committed": {"docs": False, "ideas": True}, "pushed": True})

    assert sorted(
        sh(harness.ideas, "show", "--name-status", "--no-renames", "--format=", "HEAD").splitlines()
    ) == ["A\tarchive/notes/YouTube walks.md", "D\tinbox/notes/YouTube walks.md"]
    assert sh(bare(harness.ideas), "ls-tree", "-r", "--name-only", "main", "inbox/notes").strip() == ""


def test_a_rerun_pushes_a_commit_that_a_failed_push_left_behind(harness, sh, tmp_path):
    (harness.ideas / "llm").mkdir()
    (harness.ideas / "llm/trace.json").write_text("{}\n")
    remote = str(bare(harness.ideas))
    sh(harness.ideas, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    failed = publish(harness, pull=False)

    assert isinstance(failed, Fail) and "missing.git" in failed.error
    assert head_count(sh, harness.ideas) == 2  # the commit stays local
    assert remote_log(sh, harness.ideas) == ["seed"]

    sh(harness.ideas, "remote", "set-url", "origin", remote)
    assert publish(harness) == Done({"committed": {"docs": False, "ideas": False}, "pushed": True})
    assert sh(harness.ideas, "rev-parse", "HEAD") == sh(bare(harness.ideas), "rev-parse", "main")


def test_publish_without_pull_or_push_commits_locally_only(harness, sh):
    (harness.ideas / "duplicates").mkdir()
    (harness.ideas / "duplicates/d.md").write_text("d\n")
    sh(harness.ideas, "remote", "remove", "origin")  # a pull or a push would fail now

    assert publish(harness, pull=False, push=False) == Done(
        {"committed": {"docs": False, "ideas": True}, "pushed": False}
    )
    assert head_count(sh, harness.ideas) == 2
    assert remote_log(sh, harness.ideas) == ["seed"]


def test_push_without_a_remote_fails_clearly_and_commits_nothing(frozen_harness, sh):
    (frozen_harness.ideas / "failed").mkdir(exist_ok=True)
    (frozen_harness.ideas / "failed/x.md").write_text("x\n")

    result = publish(frozen_harness)

    assert isinstance(result, Fail) and "no git remote" in result.error and "push=false" in result.error
    assert head_count(sh, frozen_harness.ideas) == 1

    assert publish(frozen_harness, push=False) == Done(
        {"committed": {"docs": False, "ideas": True}, "pushed": False}
    )


def test_bad_params_fail_the_job(harness, sh):
    for params in ({"pull": "yes"}, {"push": 1}, {"commit": True}):
        result = publish(harness, **params)
        assert isinstance(result, Fail), params
    assert head_count(sh, harness.ideas) == 1
