"""The `pipeline.publish` handler: pull both repos, commit the managed folders, push right after each commit.

Only the harness's local bare remotes in tmp folders are used: no real remote, no network. A test on the
frozen harness (no remote) passes `push=False` or expects the clear "no remote" failure."""

from pathlib import Path

from worker_harness import NOTES

from catcher.core.db import session_scope
from catcher.modules.queue.models import Job
from catcher.modules.worker.app import build_handlers
from catcher.modules.worker.handlers import Done, Fail, HandlerResult
from catcher.modules.worker.handlers_pipeline import DOCS_MANAGED, handle_pipeline_publish

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


def assert_clean_branch(sh, repo: Path) -> None:
    """Not mid-rebase, on a branch (not detached), no unmerged file and no stash entry left behind."""
    for state in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):
        assert not (repo / sh(repo, "rev-parse", "--git-path", state).strip()).exists(), state
    assert sh(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert sh(repo, "ls-files", "-u") == ""
    assert sh(repo, "stash", "list") == ""


def push_from_another_clone(sh, repo: Path, tmp_path: Path, rel: str, text: str | None) -> None:
    """Someone else changes `rel` (None deletes it) and pushes to the bare remote of `repo`."""
    other = tmp_path / f"other-{repo.name}"
    if not other.exists():
        sh(tmp_path, "clone", str(bare(repo)), str(other))
    sh(other, "pull")
    if text is None:
        (other / rel).unlink()
    else:
        (other / rel).parent.mkdir(parents=True, exist_ok=True)
        (other / rel).write_text(text)
    sh(other, "add", "-A")
    sh(other, "commit", "-m", f"remote edit of {rel}")
    sh(other, "push")


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


def test_a_failed_pull_fails_the_job_and_leaves_the_commit_on_the_branch(harness, sh, tmp_path):
    (harness.ideas / "output/notes").mkdir(parents=True)
    (harness.ideas / "output/notes/x.md").write_text("x\n")
    sh(harness.ideas, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    result = publish(harness)

    assert isinstance(result, Fail) and "git pull" in result.error
    assert_clean_branch(sh, harness.ideas)
    assert head_count(sh, harness.ideas) == 2  # committed first, then the pull failed: the commit stays
    assert sh(harness.ideas, "status", "--porcelain") == ""
    assert remote_log(sh, harness.ideas) == ["seed"]
    assert head_count(sh, harness.docs) == 1 and remote_log(sh, harness.docs) == ["seed"]  # never reached


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


def test_the_docs_commit_set_is_the_three_destination_folders_and_the_artifacts():
    assert DOCS_MANAGED == (
        "hugo/content/en/docs/idea-bucket/notes",
        "hugo/content/en/docs/idea-bucket/youtube",
        "hugo/content/en/docs/idea-bucket/web-clips",
        "idea-bucket/artifacts",
    )
    assert not any("clippings" in folder for folder in DOCS_MANAGED)


def test_a_page_in_an_old_clippings_folder_is_not_committed(harness, sh):
    root = "hugo/content/en/docs/idea-bucket"
    for folder in ("youtube", "web-clips", "clippings"):
        (harness.docs / root / folder).mkdir(parents=True, exist_ok=True)
        (harness.docs / root / folder / "20261002_abcdef_x.md").write_text(f"{folder}\n")

    assert publish(harness) == Done({"committed": {"docs": True, "ideas": False}, "pushed": True})

    assert sorted(sh(harness.docs, "show", "--name-only", "--format=", "HEAD").splitlines()) == [
        f"{root}/web-clips/20261002_abcdef_x.md",
        f"{root}/youtube/20261002_abcdef_x.md",
    ]
    assert sh(harness.docs, "status", "--porcelain").splitlines() == [f"?? {root}/clippings/"]


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


def test_an_emptied_inbox_does_not_break_the_next_publish(harness, sh):
    for path in sorted((harness.ideas / "inbox").rglob("*.md")):
        target = harness.ideas / "archive" / path.relative_to(harness.ideas / "inbox")
        target.parent.mkdir(parents=True, exist_ok=True)
        path.rename(target)
    assert publish(harness) == Done({"committed": {"docs": False, "ideas": True}, "pushed": True})
    assert (harness.ideas / "inbox/notes").is_dir()

    (harness.ideas / "llm").mkdir()
    (harness.ideas / "llm/t.json").write_text("{}\n")
    assert publish(harness) == Done({"committed": {"docs": False, "ideas": True}, "pushed": True})
    assert sh(harness.ideas, "status", "--porcelain") == ""


def test_a_conflicting_remote_edit_in_ideas_aborts_the_rebase_and_fails(harness, sh, tmp_path):
    rel = "inbox/notes/YouTube walks.md"
    push_from_another_clone(sh, harness.ideas, tmp_path, rel, "edited elsewhere\n")
    (harness.ideas / rel).write_text("edited by the worker\n")

    result = publish(harness)

    assert isinstance(result, Fail) and "rebase" in result.error
    assert_clean_branch(sh, harness.ideas)
    assert sh(harness.ideas, "log", "-1", "--format=%s").strip() == (
        "idea-catcher: process the inbox (pipeline.publish)"
    )
    assert (harness.ideas / rel).read_text() == "edited by the worker\n"
    assert remote_log(sh, harness.ideas)[0] == f"remote edit of {rel}"


def test_a_conflicting_remote_edit_in_docs_aborts_the_rebase_and_fails(harness, sh, tmp_path):
    rel = f"{NOTES}/_index.md"
    push_from_another_clone(sh, harness.docs, tmp_path, rel, "---\ntitle: Remote\n---\n")
    (harness.docs / rel).write_text("---\ntitle: Local\n---\n")

    result = publish(harness)

    assert isinstance(result, Fail) and "rebase" in result.error
    assert_clean_branch(sh, harness.docs)
    assert (
        sh(harness.docs, "log", "-1", "--format=%s").strip()
        == "idea-catcher: publish pages (pipeline.publish)"
    )
    assert (harness.docs / rel).read_text() == "---\ntitle: Local\n---\n"


def test_a_remote_edit_of_a_capture_the_worker_moved_keeps_the_deletion(harness, sh, tmp_path):
    rel = "inbox/notes/YouTube walks.md"
    push_from_another_clone(sh, harness.ideas, tmp_path, rel, "edited on the phone\n")
    (harness.ideas / "archive/notes").mkdir(parents=True)
    (harness.ideas / rel).rename(harness.ideas / "archive/notes/YouTube walks.md")

    result = publish(harness)

    # committed before the pull, so nothing is autostashed; the rebase follows the move (a rename) and puts
    # the remote edit on the archived copy: the deletion stays and the capture does not come back
    assert result == Done({"committed": {"docs": False, "ideas": True}, "pushed": True})
    assert_clean_branch(sh, harness.ideas)
    assert not (harness.ideas / rel).exists()
    assert (harness.ideas / "archive/notes/YouTube walks.md").read_text() == "edited on the phone\n"
    assert sh(bare(harness.ideas), "ls-tree", "-r", "--name-only", "main", "inbox/notes").strip() == ""
    assert sh(harness.ideas, "rev-parse", "HEAD") == sh(bare(harness.ideas), "rev-parse", "main")
    assert sh(harness.ideas, "status", "--porcelain") == ""


def test_an_unrelated_user_change_survives_a_pull_with_new_remote_commits(harness, sh, tmp_path):
    push_from_another_clone(sh, harness.ideas, tmp_path, "notes-elsewhere.md", "remote\n")
    (harness.ideas / "README.md").write_text("# edited by hand\n")
    (harness.ideas / "staged.md").write_text("s\n")
    sh(harness.ideas, "add", "staged.md")
    (harness.ideas / "facts").mkdir()
    (harness.ideas / "facts/v.json").write_text("{}\n")

    assert publish(harness) == Done({"committed": {"docs": False, "ideas": True}, "pushed": True})

    assert sh(harness.ideas, "show", "--name-only", "--format=", "HEAD").splitlines() == ["facts/v.json"]
    assert sorted(sh(harness.ideas, "status", "--porcelain").splitlines()) == [" M README.md", "A  staged.md"]
    assert sh(harness.ideas, "rev-parse", "HEAD") == sh(bare(harness.ideas), "rev-parse", "main")
    assert_clean_branch(sh, harness.ideas)


def test_a_branch_without_an_upstream_fails_clearly(harness, sh):
    (harness.ideas / "facts").mkdir()
    (harness.ideas / "facts/v.json").write_text("{}\n")
    sh(harness.ideas, "branch", "--unset-upstream")

    result = publish(harness, pull=False)

    assert isinstance(result, Fail) and "no upstream" in result.error


def test_a_rejected_push_whose_rebase_conflicts_is_aborted(harness, sh, tmp_path):
    rel = "inbox/notes/YouTube walks.md"
    push_from_another_clone(sh, harness.ideas, tmp_path, rel, "edited elsewhere\n")
    sh(harness.ideas, "fetch")  # the clone knows the remote moved on, so it is not 'ahead' only
    (harness.ideas / rel).write_text("edited by the worker\n")

    result = publish(harness, pull=False)  # the push is rejected; its retry rebases and conflicts

    assert isinstance(result, Fail) and "rebase" in result.error
    assert_clean_branch(sh, harness.ideas)
    assert (harness.ideas / rel).read_text() == "edited by the worker\n"


def test_a_capture_pushed_into_a_folder_the_worker_emptied_is_pulled_in(harness, sh, tmp_path):
    notes = harness.ideas / "inbox/notes"
    for path in sorted(notes.glob("*.md")):
        (harness.ideas / "archive/notes").mkdir(parents=True, exist_ok=True)
        path.rename(harness.ideas / "archive/notes" / path.name)
    push_from_another_clone(sh, harness.ideas, tmp_path, "inbox/notes/new.md", "from the phone\n")

    assert publish(harness) == Done({"committed": {"docs": False, "ideas": True}, "pushed": True})

    assert (notes / "new.md").read_text() == "from the phone\n"
    assert not (harness.ideas / "archive/notes/new.md").exists()
    assert_clean_branch(sh, harness.ideas)
    assert sh(harness.ideas, "rev-parse", "HEAD") == sh(bare(harness.ideas), "rev-parse", "main")


def test_every_git_call_of_publish_runs_unattended(harness, monkeypatch):
    from catcher.core import git as gitmod

    seen: list[bool] = []
    real_git = gitmod.git

    def recording_git(repo, *args, unattended=False):
        seen.append(unattended)
        return real_git(repo, *args, unattended=unattended)

    monkeypatch.setattr(gitmod, "git", recording_git)
    (harness.ideas / "facts").mkdir()
    (harness.ideas / "facts/v.json").write_text("{}\n")

    assert publish(harness) == Done({"committed": {"docs": False, "ideas": True}, "pushed": True})
    assert seen and all(seen)
