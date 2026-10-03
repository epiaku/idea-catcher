"""The git runner: interactive by default (Stage A's CLI); unattended (the worker): timeouts, no prompts."""

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


def test_the_default_runner_is_interactive_as_in_stage_a(monkeypatch, tmp_path):
    """Stage A's CLI: no timeout, no environment override, the terminal stays available for prompts."""
    rec = Recorder()
    monkeypatch.setattr(gitmod.subprocess, "run", rec)
    gitmod.git(tmp_path, "status", "--porcelain")
    gitmod.git(tmp_path, "push")
    for cmd, kwargs in rec.calls:
        assert kwargs == {"cwd": tmp_path, "capture_output": True, "text": True}, cmd


def test_unattended_local_commands_get_a_short_timeout_and_network_commands_a_long_one(monkeypatch, tmp_path):
    rec = Recorder()
    monkeypatch.setattr(gitmod, "_run_unattended", rec)
    gitmod.git(tmp_path, "status", "--porcelain", unattended=True)
    gitmod.git(tmp_path, "-c", "user.name=x", "commit", "-m", "m", unattended=True)
    for net in ("pull", "push", "fetch"):
        gitmod.git(tmp_path, "-c", "http.lowSpeedLimit=1000", net, unattended=True)
    timeouts = [kwargs["timeout"] for _, kwargs in rec.calls]
    assert timeouts == [gitmod.LOCAL_TIMEOUT_S] * 2 + [gitmod.NETWORK_TIMEOUT_S] * 3
    assert gitmod.LOCAL_TIMEOUT_S == 30 and gitmod.NETWORK_TIMEOUT_S == 600


def test_unattended_git_never_prompts_and_keeps_the_users_ssh_command(monkeypatch, tmp_path):
    rec = Recorder()
    monkeypatch.setattr(gitmod, "_run_unattended", rec)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    gitmod.git(tmp_path, "status", unattended=True)
    env = rec.calls[-1][1]["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_SSH_COMMAND" not in env  # a local command runs no ssh
    gitmod.git(tmp_path, "push", unattended=True)
    assert rec.calls[-1][1]["env"]["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes -o ServerAliveInterval=15"

    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i my-key")
    gitmod.git(tmp_path, "push", unattended=True)
    assert rec.calls[-1][1]["env"]["GIT_SSH_COMMAND"] == "ssh -i my-key"


def test_the_unattended_runner_has_no_terminal_and_its_own_process_group(monkeypatch, tmp_path):
    seen: dict = {}
    real_popen = subprocess.Popen

    def recording_popen(cmd, **kwargs):
        seen.update(kwargs)
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(gitmod.subprocess, "Popen", recording_popen)
    gitmod.git(tmp_path, "--version", unattended=True)
    assert seen["stdin"] is subprocess.DEVNULL and seen["start_new_session"] is True


def pull_command(monkeypatch, tmp_path, **kwargs) -> list[str]:
    rec = Recorder()
    monkeypatch.setattr(gitmod.subprocess, "run", rec)
    monkeypatch.setattr(gitmod, "_run_unattended", rec)
    pull(tmp_path, **kwargs)
    [(cmd, _)] = [c for c in rec.calls if "pull" in c[0]]
    return cmd


def test_an_unattended_pull_stops_a_stalled_http_transfer(monkeypatch, tmp_path):
    monkeypatch.setattr(gitmod, "has_remote", lambda repo, **kwargs: True)
    cmd = pull_command(monkeypatch, tmp_path, unattended=True)
    assert cmd[:5] == ["git", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60"]
    assert "merge.directoryRenames=false" in cmd
    assert cmd[-3:] == ["pull", "--rebase", "--autostash"]


def test_the_default_pull_keeps_only_the_correctness_options(monkeypatch, tmp_path):
    monkeypatch.setattr(gitmod, "has_remote", lambda repo, **kwargs: True)
    cmd = pull_command(monkeypatch, tmp_path)
    assert cmd == ["git", "-c", "merge.directoryRenames=false", "pull", "--rebase", "--autostash"]


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

    def git_with_a_failing_abort(repo, *args, **kwargs):
        if "--abort" in args:
            raise GitError("git rebase --abort failed: index.lock exists")
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(gitmod, "git", git_with_a_failing_abort)
    with pytest.raises(GitError) as raised:
        pull(work)
    message = str(raised.value)
    assert "git pull" in message and "failed in" in message
    assert "index.lock exists" in message and "still in the middle of a rebase" in message
    sh(work, "rebase", "--abort")  # leave the tmp repo tidy


def test_a_timeout_stops_the_whole_process_group_and_leaves_no_lock(make_repo, sh, monkeypatch):
    import os
    import time

    from catcher.core.git import commit_managed

    _, work = make_repo("ideas", {})
    pid_file = work.parent / "hook.pid"
    hook = work / ".git/hooks/pre-commit"
    hook.write_text(f"#!/bin/sh\necho $$ > '{pid_file}'\nsleep 30\n")
    hook.chmod(0o755)
    (work / "facts").mkdir()
    (work / "facts/v.json").write_text("{}\n")
    monkeypatch.setattr(gitmod, "LOCAL_TIMEOUT_S", 2)
    monkeypatch.setattr(gitmod, "KILL_GRACE_S", 3)

    started = time.monotonic()
    with pytest.raises(GitError, match="timed out"):
        commit_managed(work, ["facts"], "slow", author=AUTHOR, unattended=True)
    assert time.monotonic() - started < 10

    assert not (work / ".git/index.lock").exists()
    assert not list((work / ".git").glob("*.lock"))
    hook_pid = int(pid_file.read_text())
    for _ in range(50):  # the hook was in the group: it is stopped too (allow a moment to be reaped)
        try:
            os.kill(hook_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the hook outlived the timeout")

    hook.unlink()
    assert commit_managed(work, ["facts"], "fast", author=AUTHOR, unattended=True) is True


def network_env(monkeypatch, repo: Path) -> dict:
    """The env an unattended push gets in `repo`."""
    return gitmod.unattended_env(repo, ("push",))


def test_unattended_ssh_gets_batch_mode_only_when_the_user_set_no_ssh_command(make_repo, sh, monkeypatch):
    _, work = make_repo("ideas", {})
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    assert network_env(monkeypatch, work)["GIT_SSH_COMMAND"] == gitmod.SSH_COMMAND


def test_unattended_ssh_leaves_the_users_core_ssh_command_alone(make_repo, sh, monkeypatch):
    _, work = make_repo("ideas", {})
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    sh(work, "config", "core.sshCommand", "ssh -i deploy-key")
    env = network_env(monkeypatch, work)
    assert "GIT_SSH_COMMAND" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_unattended_ssh_leaves_the_users_git_ssh_alone(make_repo, sh, monkeypatch):
    _, work = make_repo("ideas", {})
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setenv("GIT_SSH", "/usr/local/bin/my-ssh")
    env = network_env(monkeypatch, work)
    assert "GIT_SSH_COMMAND" not in env and env["GIT_SSH"] == "/usr/local/bin/my-ssh"


def test_in_head_lets_a_git_failure_through(make_repo, sh, monkeypatch):
    from catcher.core.git import GitTimeout, commit_managed

    _, work = make_repo("ideas", {"inbox/n.md": "n\n"})
    real_git = gitmod.git

    def slow_ls_tree(repo, *args, **kwargs):
        if "ls-tree" in args:
            raise GitTimeout("git ls-tree -r --name-only timed out")
        return real_git(repo, *args, **kwargs)

    sh(work, "rm", "-q", "inbox/n.md")  # a staged deletion: only HEAD still knows the folder
    monkeypatch.setattr(gitmod, "git", slow_ls_tree)
    with pytest.raises(GitTimeout):
        commit_managed(work, ["inbox"], "m", author=AUTHOR, unattended=True)


def test_in_head_is_false_in_a_repo_without_a_commit(tmp_path, sh):
    from catcher.core.git import commit_managed

    sh(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts/v.json").write_text("{}\n")
    assert commit_managed(tmp_path, ["facts", "inbox"], "first", author=AUTHOR, unattended=True) is True
