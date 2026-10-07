"""The `ideas.pull` handler: pull (rebase, autostash) the idea-bucket checkout.

Only the harness's local bare remotes in tmp folders are used: no real remote, no network."""

from pathlib import Path

import pytest

from catcher.core.db import session_scope
from catcher.modules.queue.models import Job
from catcher.modules.worker.app import build_handlers, check_job
from catcher.modules.worker.handlers import Done, Fail, HandlerResult
from catcher.modules.worker.handlers_pipeline import handle_ideas_pull


def assert_clean_branch(sh, repo: Path) -> None:
    """Not mid-rebase, on a branch (not detached), no unmerged file and no stash entry left behind."""
    for state in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):
        assert not (repo / sh(repo, "rev-parse", "--git-path", state).strip()).exists(), state
    assert sh(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert sh(repo, "ls-files", "-u") == ""
    assert sh(repo, "stash", "list") == ""


def push_from_another_clone(sh, repo: Path, tmp_path: Path, rel: str, text: str) -> None:
    """Someone else changes `rel` and pushes to the bare remote of `repo` (`<name>.git` next to it)."""
    other = tmp_path / f"other-{repo.name}"
    if not other.exists():
        sh(tmp_path, "clone", str(repo.with_name(f"{repo.name}.git")), str(other))
    sh(other, "pull")
    (other / rel).parent.mkdir(parents=True, exist_ok=True)
    (other / rel).write_text(text)
    sh(other, "add", "-A")
    sh(other, "commit", "-m", f"remote edit of {rel}")
    sh(other, "push")


def pull_job(harness) -> HandlerResult:
    job_id = harness.add_job("ideas.pull")
    with session_scope(harness.ctx.engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return handle_ideas_pull(harness.ctx, job)


def test_pull_brings_a_new_capture_from_the_remote(harness, sh, tmp_path):
    push_from_another_clone(sh, harness.ideas, tmp_path, "inbox/notes/phone.md", "from the phone\n")

    result = pull_job(harness)

    assert result == Done({"pulled": True})
    assert (harness.ideas / "inbox/notes/phone.md").read_text() == "from the phone\n"
    assert_clean_branch(sh, harness.ideas)


def test_pull_without_a_remote_is_a_noop_done(harness, sh):
    sh(harness.ideas, "remote", "remove", "origin")

    result = pull_job(harness)

    assert isinstance(result, Done)
    assert result.result == {"pulled": False, "reason": "no git remote"}


def test_pull_keeps_a_dirty_working_copy(harness, sh, tmp_path):
    push_from_another_clone(sh, harness.ideas, tmp_path, "inbox/notes/phone.md", "from the phone\n")
    (harness.ideas / "scratch.txt").write_text("mine\n")
    (harness.ideas / "inbox/notes").mkdir(parents=True, exist_ok=True)
    (harness.ideas / "inbox/notes/local.md").write_text("local\n")

    assert pull_job(harness) == Done({"pulled": True})

    assert (harness.ideas / "scratch.txt").read_text() == "mine\n"
    assert (harness.ideas / "inbox/notes/local.md").read_text() == "local\n"
    assert (harness.ideas / "inbox/notes/phone.md").exists()
    assert_clean_branch(sh, harness.ideas)


def test_a_conflict_fails_the_job_and_leaves_no_rebase_in_progress(harness, sh, tmp_path):
    (harness.ideas / "clash.md").write_text("base\n")
    sh(harness.ideas, "add", "-A")
    sh(harness.ideas, "commit", "-m", "base")
    sh(harness.ideas, "push")
    push_from_another_clone(sh, harness.ideas, tmp_path, "clash.md", "theirs\n")
    (harness.ideas / "clash.md").write_text("ours\n")
    sh(harness.ideas, "add", "-A")
    sh(harness.ideas, "commit", "-m", "ours")

    result = pull_job(harness)

    assert isinstance(result, Fail) and result.error
    assert_clean_branch(sh, harness.ideas)
    assert (harness.ideas / "clash.md").read_text() == "ours\n"


def test_ideas_pull_refuses_params_and_is_in_the_registry():
    assert build_handlers()["ideas.pull"] is handle_ideas_pull
    check_job("ideas.pull", {})
    with pytest.raises(ValueError, match="ideas.pull"):
        check_job("ideas.pull", {"x": 1})


def test_pull_leaves_a_hand_started_rebase_alone(harness, sh, tmp_path):
    (harness.ideas / "clash.md").write_text("base\n")
    sh(harness.ideas, "add", "-A")
    sh(harness.ideas, "commit", "-m", "base")
    sh(harness.ideas, "push")
    push_from_another_clone(sh, harness.ideas, tmp_path, "clash.md", "theirs\n")
    (harness.ideas / "clash.md").write_text("ours\n")
    sh(harness.ideas, "add", "-A")
    sh(harness.ideas, "commit", "-m", "ours")
    sh(harness.ideas, "fetch")
    with pytest.raises(Exception):  # noqa: B017 - the rebase stops on the conflict: the user's work in progress
        sh(harness.ideas, "rebase", "origin/main")
    conflicted = (harness.ideas / "clash.md").read_text()
    assert "<<<<<<<" in conflicted

    result = pull_job(harness)

    assert isinstance(result, Fail) and "rebase" in result.error
    git_dir = harness.ideas / ".git"
    assert (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()
    assert (harness.ideas / "clash.md").read_text() == conflicted


def test_pull_on_a_detached_head_fails_clearly(harness, sh):
    sh(harness.ideas, "checkout", "--detach")

    result = pull_job(harness)

    assert isinstance(result, Fail) and "detached" in result.error


def seed_worker_files(harness, sh) -> None:
    """`output/y.md` and `inbox/x.md` are tracked and pushed."""
    for rel in ("output/y.md", "inbox/x.md"):
        (harness.ideas / rel).parent.mkdir(parents=True, exist_ok=True)
        (harness.ideas / rel).write_text("base\n")
    sh(harness.ideas, "add", "-A")
    sh(harness.ideas, "commit", "-m", "seed files")
    sh(harness.ideas, "push")


def test_pull_when_the_remote_edited_a_file_the_worker_changed_leaves_the_files_intact(harness, sh, tmp_path):
    seed_worker_files(harness, sh)
    push_from_another_clone(sh, harness.ideas, tmp_path, "output/y.md", "remote y\n")
    push_from_another_clone(sh, harness.ideas, tmp_path, "inbox/x.md", "remote x\n")
    (harness.ideas / "output/y.md").write_text("worker y\n")  # uncommitted, as after a pipeline.run
    (harness.ideas / "inbox/x.md").unlink()

    result = pull_job(harness)

    assert isinstance(result, Fail) and result.error
    assert_clean_branch(sh, harness.ideas)  # no rebase, no unmerged file, no stash entry
    assert (harness.ideas / "output/y.md").read_text() == "worker y\n"  # no conflict markers
    assert not (harness.ideas / "inbox/x.md").exists()
    assert "unmerged" not in sh(harness.ideas, "status").lower()

    # the remote edit is gone: the next pull succeeds with the worker's commit ahead
    other = tmp_path / f"other-{harness.ideas.name}"
    sh(other, "reset", "--hard", "HEAD~2")
    sh(other, "push", "--force")
    assert pull_job(harness) == Done({"pulled": True})
    assert (harness.ideas / "output/y.md").read_text() == "worker y\n"


def test_pull_commits_the_workers_changes_locally_first_and_does_not_push(harness, sh):
    seed_worker_files(harness, sh)
    remote = harness.ideas.with_name(f"{harness.ideas.name}.git")
    remote_head = sh(remote, "rev-parse", "main")
    (harness.ideas / "output/y.md").write_text("worker y\n")

    assert pull_job(harness) == Done({"pulled": True})

    assert sh(harness.ideas, "status", "--porcelain") == ""
    assert (
        sh(harness.ideas, "log", "-1", "--format=%s").strip()
        == "idea-catcher: process the inbox (pipeline.publish)"
    )
    assert sh(remote, "rev-parse", "main") == remote_head
