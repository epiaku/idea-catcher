"""The git runner used by publish: timeouts per kind of command, no prompts, and the pull's own identity."""

import subprocess
from pathlib import Path

import pytest

from catcher.core import git as gitmod
from catcher.core.git import GitError, pull

AUTHOR = ("idea-catcher", "bot@example.com")


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_local_commands_get_a_short_timeout_and_network_commands_a_long_one(monkeypatch, tmp_path):
    rec = Recorder()
    monkeypatch.setattr(gitmod.subprocess, "run", rec)
    gitmod.git(tmp_path, "status", "--porcelain")
    gitmod.git(tmp_path, "-c", "user.name=x", "commit", "-m", "m")
    for net in ("pull", "push", "fetch"):
        gitmod.git(tmp_path, "-c", "http.lowSpeedLimit=1000", net)
    timeouts = [kwargs["timeout"] for _, kwargs in rec.calls]
    assert timeouts == [gitmod.LOCAL_TIMEOUT_S] * 2 + [gitmod.NETWORK_TIMEOUT_S] * 3
    assert gitmod.LOCAL_TIMEOUT_S == 30 and gitmod.NETWORK_TIMEOUT_S == 600


def test_git_never_prompts_and_keeps_the_users_ssh_command(monkeypatch, tmp_path):
    rec = Recorder()
    monkeypatch.setattr(gitmod.subprocess, "run", rec)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    gitmod.git(tmp_path, "status")
    env = rec.calls[-1][1]["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes -o ServerAliveInterval=15"
    assert rec.calls[-1][1]["stdin"] is subprocess.DEVNULL

    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i my-key")
    gitmod.git(tmp_path, "status")
    assert rec.calls[-1][1]["env"]["GIT_SSH_COMMAND"] == "ssh -i my-key"


def test_network_commands_stop_a_stalled_http_transfer(monkeypatch, tmp_path):
    rec = Recorder()
    monkeypatch.setattr(gitmod.subprocess, "run", rec)
    monkeypatch.setattr(gitmod, "has_remote", lambda repo: True)
    pull(tmp_path)
    [(cmd, _)] = [c for c in rec.calls if "pull" in c[0]]
    assert cmd[:5] == ["git", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60"]
    assert "merge.directoryRenames=false" in cmd
    assert cmd[-3:] == ["pull", "--rebase", "--autostash"]


def diverged(make_repo, sh, tmp_path: Path) -> Path:
    """A clone with a local commit and a remote commit on another file: its pull has to rebase."""
    bare, work = make_repo("ideas", {"a.md": "a\n", "b.md": "b\n"})
    other = tmp_path / "other"
    sh(tmp_path, "clone", str(bare), str(other))
    (other / "b.md").write_text("b remote\n")
    sh(other, "commit", "-am", "remote")
    sh(other, "push")
    (work / "a.md").write_text("a local\n")
    sh(work, "commit", "-am", "local")
    return work


def test_the_pull_rebases_with_the_given_identity(make_repo, sh, tmp_path, monkeypatch):
    work = diverged(make_repo, sh, tmp_path)
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(key)
    sh(work, "config", "user.useConfigOnly", "true")  # no identity at all, like a bare container

    pull(work, author=AUTHOR)

    assert sh(work, "log", "-1", "--format=%cn <%ce>|%s").strip() == "idea-catcher <bot@example.com>|local"
    assert sh(work, "log", "--format=%s").splitlines() == ["local", "remote", "seed"]


def test_a_failed_abort_keeps_the_pull_error(make_repo, sh, tmp_path, monkeypatch):
    bare, work = make_repo("ideas", {"a.md": "a\n"})
    other = tmp_path / "other"
    sh(tmp_path, "clone", str(bare), str(other))
    (other / "a.md").write_text("remote\n")
    sh(other, "commit", "-am", "remote")
    sh(other, "push")
    (work / "a.md").write_text("local\n")
    sh(work, "commit", "-am", "local")

    real_git = gitmod.git

    def git_with_a_failing_abort(repo, *args):
        if "--abort" in args:
            raise GitError("git rebase --abort failed: index.lock exists")
        return real_git(repo, *args)

    monkeypatch.setattr(gitmod, "git", git_with_a_failing_abort)
    with pytest.raises(GitError) as raised:
        pull(work)
    message = str(raised.value)
    assert "git pull" in message and "failed in" in message
    assert "index.lock exists" in message and "still in the middle of a rebase" in message
    sh(work, "rebase", "--abort")  # leave the tmp repo tidy
