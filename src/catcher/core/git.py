import contextlib
import logging
import os
import signal
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

log = logging.getLogger("catcher.git")

# Process-local and held only by the worker's `pipeline.publish` (around commit, pull and push). Not
# reentrant, and no protection against another process (a `catcher run pipeline` at the same time).
GIT_LOCK = threading.Lock()
# The unattended settings below apply only to calls with `unattended=True` (the worker's pipeline.publish).
# Stage A's CLI keeps git interactive: password and passphrase prompts, no timeouts.
LOCAL_TIMEOUT_S = 30  # status, add, commit, rebase --abort ...: a hang there is a bug or a stuck lock
NETWORK_TIMEOUT_S = 600  # pull, push, fetch: a backstop; a stalled transfer is stopped by git itself first
NETWORK_COMMANDS = frozenset({"pull", "push", "fetch"})
# a stalled HTTP transfer (under 1000 bytes/s for 60 s) is stopped by git, long before the wall clock
NETWORK_OPTIONS = ("-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60")
SSH_COMMAND = "ssh -o BatchMode=yes -o ServerAliveInterval=15"  # no passphrase or host-key prompt, no hang
KILL_GRACE_S = 10  # after a timeout: SIGTERM to the process group, this long to clean up, then SIGKILL


class GitError(RuntimeError):
    pass


class GitTimeout(GitError):
    """An unattended git command ran out of time and was stopped."""


def _command(args: tuple[str, ...]) -> list[str]:
    """The command without the leading `-c key=value` options."""
    rest = list(args)
    while len(rest) >= 2 and rest[0] == "-c":
        rest = rest[2:]
    return rest


def _shown(args: tuple[str, ...]) -> str:
    """The command for a message, without the leading `-c key=value` options."""
    return " ".join(_command(args)[:3])


def _identity(author: tuple[str, str] | None) -> tuple[str, ...]:
    if author is None:
        return ()
    name, email = author
    return ("-c", f"user.name={name}", "-c", f"user.email={email}")


def _ssh_configured(repo: Path, env: dict[str, str]) -> bool:
    """True when the user chose how git runs ssh (GIT_SSH_COMMAND, GIT_SSH or core.sshCommand)."""
    if env.get("GIT_SSH_COMMAND") or env.get("GIT_SSH"):
        return True
    out = subprocess.run(
        ["git", "config", "--get", "core.sshCommand"],
        cwd=repo,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
        timeout=LOCAL_TIMEOUT_S,
    )
    return out.returncode == 0 and bool(out.stdout.strip())


def unattended_env(repo: Path, args: tuple[str, ...]) -> dict[str, str]:
    """The environment of an unattended git command: GIT_TERMINAL_PROMPT=0, and for a network command ssh in
    batch mode, but only when the user did not choose an ssh command of their own (that one is left alone)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    command = _command(args)
    if command and command[0] in NETWORK_COMMANDS and not _ssh_configured(repo, env):
        env["GIT_SSH_COMMAND"] = SSH_COMMAND
    return env


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)


def _run_unattended(
    cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run `cmd` in its own process group with no terminal. On a timeout the whole group (git, its hooks,
    ssh) gets SIGTERM, so git removes its lock files; whatever is left after KILL_GRACE_S gets SIGKILL.
    Raises GitTimeout once the child is reaped."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _signal_group(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            _signal_group(proc.pid, signal.SIGKILL)
            proc.communicate()
        _signal_group(proc.pid, signal.SIGKILL)  # a grandchild that ignored SIGTERM
        raise GitTimeout(f"timed out after {timeout} s") from None
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def git(repo: Path, *args: str, unattended: bool = False) -> str:
    """Run git in `repo`; raises GitError. By default as an interactive user would (the terminal stays
    available for a password or passphrase prompt, no timeout). `unattended=True` (the worker) runs with no
    terminal, GIT_TERMINAL_PROMPT=0, ssh in batch mode unless the user set an ssh command (`unattended_env`),
    and a timeout (NETWORK_TIMEOUT_S for pull, push and fetch, LOCAL_TIMEOUT_S for the rest) that stops the
    whole process group: a GitTimeout."""
    if unattended:
        command = _command(args)
        timeout = NETWORK_TIMEOUT_S if command and command[0] in NETWORK_COMMANDS else LOCAL_TIMEOUT_S
        try:
            out = _run_unattended(["git", *args], cwd=repo, env=unattended_env(repo, args), timeout=timeout)
        except GitTimeout as e:
            message = f"git {_shown(args)} timed out after {timeout} s in {repo} and was stopped"
            log.error(message)
            raise GitTimeout(message) from e
    else:
        out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if out.returncode != 0:
        message = f"git {_shown(args)} failed in {repo}: {out.stderr.strip()[:500]}"
        log.error(message)
        raise GitError(message)
    return out.stdout


def _network(unattended: bool) -> tuple[str, ...]:
    return NETWORK_OPTIONS if unattended else ()


def has_remote(repo: Path, *, unattended: bool = False) -> bool:
    return bool(git(repo, "remote", unattended=unattended).strip())


def _git_path(repo: Path, name: str, unattended: bool) -> Path:
    return repo / git(repo, "rev-parse", "--git-path", name, unattended=unattended).strip()


def _rebase_in_progress(repo: Path, unattended: bool) -> bool:
    return (
        _git_path(repo, "rebase-merge", unattended).exists()
        or _git_path(repo, "rebase-apply", unattended).exists()
    )


def _pull_rebase(repo: Path, author: tuple[str, str] | None = None, *, unattended: bool = False) -> None:
    """`git pull --rebase --autostash`, rebasing with `author` as the committer when given (no global
    identity needed) and without directory-rename detection (a capture another device adds to a folder the
    worker emptied stays where it was added). A failed rebase is aborted (the branch, the commits and the
    stashed changes come back as they were) before the GitError is raised. Changes the autostash could not
    put back cleanly raise a GitError too: they are left in the working tree and in `git stash list`."""
    try:
        git(
            repo,
            *_network(unattended),
            *_identity(author),
            "-c",
            "merge.directoryRenames=false",
            "pull",
            "--rebase",
            "--autostash",
            unattended=unattended,
        )
    except GitError as e:
        if not _rebase_in_progress(repo, unattended):
            raise
        try:
            git(repo, "rebase", "--abort", unattended=unattended)
        except GitError as abort_error:
            log.error("rebase --abort failed in %s: %s", repo, abort_error)
            raise GitError(
                f"{e} -- and the abort failed too ({abort_error}): {repo} is still in the middle of a "
                "rebase; run `git rebase --abort` by hand"
            ) from e
        raise GitError(
            f"{e} -- the rebase was aborted: the local commits are kept and not pushed; "
            "settle the conflict with the remote by hand"
        ) from e
    if git(repo, "ls-files", "-u", unattended=unattended).strip():
        raise GitError(
            f"pull in {repo}: the uncommitted changes git stashed for the pull conflict with the pulled "
            "commits; they are in the working tree and in `git stash list`: settle them by hand"
        )


def pull(repo: Path, *, author: tuple[str, str] | None = None, unattended: bool = False) -> None:
    if has_remote(repo, unattended=unattended):
        log.info("pull %s", repo.name)
        _pull_rebase(repo, author, unattended=unattended)


def _commit(
    repo: Path, message: str, paths: Sequence[str], *, author: tuple[str, str], unattended: bool = False
) -> None:
    name, email = author
    git(
        repo,
        *_identity(author),
        "commit",
        "--only",
        f"--author={name} <{email}>",
        "-m",
        message,
        "--",
        *paths,
        unattended=unattended,
    )


def require_on_branch(repo: Path, *, unattended: bool = False) -> None:
    """Raise GitError unless `repo` is on a branch with no rebase or merge going on and no unmerged file."""
    if _rebase_in_progress(repo, unattended) or _git_path(repo, "MERGE_HEAD", unattended).exists():
        raise GitError(f"{repo} is in the middle of a rebase or a merge: finish or abort it by hand")
    try:
        git(repo, "symbolic-ref", "-q", "HEAD", unattended=unattended)
    except GitError as e:
        raise GitError(f"{repo} is not on a branch (detached HEAD): check out the branch by hand") from e
    if git(repo, "ls-files", "-u", unattended=unattended).strip():
        raise GitError(f"{repo} has unmerged files: settle them by hand")


def _in_head(repo: Path, path: str, unattended: bool) -> bool:
    """True when HEAD has files under `path` (a deletion staged by a failed commit is only there). False in a
    repo without a commit; any other git failure (a timeout included) is raised."""
    try:
        git(repo, "rev-parse", "--verify", "-q", "HEAD", unattended=unattended)
    except GitTimeout:
        raise
    except GitError:  # no commit yet
        return False
    return bool(git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", path, unattended=unattended).strip())


def commit_managed(
    repo: Path, pathspecs: Sequence[str], message: str, *, author: tuple[str, str], unattended: bool = False
) -> bool:
    """Commit everything under `pathspecs` (folders relative to `repo`): new, changed and deleted files, so a
    note moved out of `inbox/` is committed as a deletion. Only pathspecs git knows are used (tracked files,
    or untracked files that are not ignored): a missing, empty or ignored-only folder is skipped. Changes
    outside `pathspecs` (in the working tree or the index) are left as they are. Returns False, and makes no
    commit, when nothing under `pathspecs` changed. A path in HEAD is kept too, so a deletion a failed commit
    left staged is committed by the next call. Raises GitError when the repo is not on a clean branch."""
    require_on_branch(repo, unattended=unattended)
    known = [
        p
        for p in dict.fromkeys(pathspecs)
        if git(
            repo, "ls-files", "--cached", "--others", "--exclude-standard", "--", p, unattended=unattended
        ).strip()
        or _in_head(repo, p, unattended)
    ]
    if not known:
        return False
    git(repo, "add", "-A", "--", *known, unattended=unattended)
    changed = git(repo, "diff", "--cached", "--name-only", "--", *known, unattended=unattended).splitlines()
    if not changed:
        return False
    log.info("commit %s: %s (%d file(s))", repo.name, message, len(changed))
    _commit(repo, message, known, author=author, unattended=unattended)
    return True


def ahead_of_upstream(repo: Path, *, unattended: bool = False) -> bool:
    """True when the branch has commits its upstream lacks. Raises GitError when it has no upstream."""
    try:
        git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", unattended=unattended)
    except GitError as e:
        raise GitError(
            f"the branch in {repo} has no upstream to push to: set one with `git push -u origin <branch>`"
        ) from e
    return git(repo, "rev-list", "--count", "@{upstream}..HEAD", unattended=unattended).strip() != "0"


def push(repo: Path, *, author: tuple[str, str] | None = None, unattended: bool = False) -> None:
    if not has_remote(repo, unattended=unattended):
        return
    log.info("push %s", repo.name)
    try:
        git(repo, *_network(unattended), "push", unattended=unattended)
    except GitError:
        log.warning("push %s was rejected, retrying once after a rebase", repo.name)
        _pull_rebase(repo, author, unattended=unattended)
        git(repo, *_network(unattended), "push", unattended=unattended)
