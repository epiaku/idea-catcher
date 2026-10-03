import logging
import os
import subprocess
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path

log = logging.getLogger("catcher.git")

# Process-local and held only by the worker's `pipeline.publish` (around commit, pull and push). Not
# reentrant, and no protection against another process (a `catcher run pipeline` at the same time).
GIT_LOCK = threading.Lock()
GIT_TIMEOUT_S = 120  # a pull or push that hangs (a dead remote) must not hold the worker for ever


class GitError(RuntimeError):
    pass


def _shown(args: tuple[str, ...]) -> str:
    """The command for a message, without the leading `-c key=value` options."""
    rest = list(args)
    while len(rest) >= 2 and rest[0] == "-c":
        rest = rest[2:]
    return " ".join(rest[:3])


def git(repo: Path, *args: str) -> str:
    """Run git in `repo`: no prompt (no terminal, GIT_TERMINAL_PROMPT=0) and a timeout. Raises GitError."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        message = f"git {_shown(args)} timed out after {GIT_TIMEOUT_S} s in {repo}"
        log.error(message)
        raise GitError(message) from e
    if out.returncode != 0:
        message = f"git {_shown(args)} failed in {repo}: {out.stderr.strip()[:500]}"
        log.error(message)
        raise GitError(message)
    return out.stdout


def has_remote(repo: Path) -> bool:
    return bool(git(repo, "remote").strip())


def _git_path(repo: Path, name: str) -> Path:
    return repo / git(repo, "rev-parse", "--git-path", name).strip()


def _rebase_in_progress(repo: Path) -> bool:
    return _git_path(repo, "rebase-merge").exists() or _git_path(repo, "rebase-apply").exists()


def _pull_rebase(repo: Path) -> None:
    """`git pull --rebase --autostash`. A failed rebase is aborted (the branch, the commits and the stashed
    changes come back as they were) before the GitError is raised. Changes the autostash could not put back
    cleanly raise a GitError too: they are left in the working tree and in `git stash list` for a human."""
    try:
        git(repo, "pull", "--rebase", "--autostash")
    except GitError as e:
        if not _rebase_in_progress(repo):
            raise
        git(repo, "rebase", "--abort")
        raise GitError(
            f"{e} -- the rebase was aborted: the local commits are kept and not pushed; "
            "settle the conflict with the remote by hand"
        ) from e
    if git(repo, "ls-files", "-u").strip():
        raise GitError(
            f"pull in {repo}: the uncommitted changes git stashed for the pull conflict with the pulled "
            "commits; they are in the working tree and in `git stash list`: settle them by hand"
        )


def pull(repo: Path) -> None:
    if has_remote(repo):
        log.info("pull %s", repo.name)
        _pull_rebase(repo)


def _relative(repo: Path, paths: Iterable[Path]) -> list[str]:
    root = repo.resolve()
    return sorted({p.resolve().relative_to(root).as_posix() for p in paths})


def commit_paths(repo: Path, paths: Iterable[Path], message: str, *, author: tuple[str, str]) -> bool:
    rels = _relative(repo, paths)
    if not rels:
        return False
    existing = [rel for rel in rels if (repo / rel).exists()]
    if existing:
        git(repo, "add", "--", *existing)
    in_index = [rel for rel in git(repo, "ls-files", "-z", "--", *rels).split("\0") if rel]
    if not in_index or not git(repo, "status", "--porcelain", "--", *in_index).strip():
        return False
    log.info("commit %s: %s (%d file(s))", repo.name, message, len(in_index))
    _commit(repo, message, in_index, author=author)
    return True


def _commit(repo: Path, message: str, paths: Sequence[str], *, author: tuple[str, str]) -> None:
    name, email = author
    git(
        repo,
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit",
        "--only",
        f"--author={name} <{email}>",
        "-m",
        message,
        "--",
        *paths,
    )


def require_on_branch(repo: Path) -> None:
    """Raise GitError unless `repo` is on a branch with no rebase or merge going on and no unmerged file."""
    if _rebase_in_progress(repo) or _git_path(repo, "MERGE_HEAD").exists():
        raise GitError(f"{repo} is in the middle of a rebase or a merge: finish or abort it by hand")
    try:
        git(repo, "symbolic-ref", "-q", "HEAD")
    except GitError as e:
        raise GitError(f"{repo} is not on a branch (detached HEAD): check out the branch by hand") from e
    if git(repo, "ls-files", "-u").strip():
        raise GitError(f"{repo} has unmerged files: settle them by hand")


def commit_managed(repo: Path, pathspecs: Sequence[str], message: str, *, author: tuple[str, str]) -> bool:
    """Commit everything under `pathspecs` (folders relative to `repo`): new, changed and deleted files, so a
    note moved out of `inbox/` is committed as a deletion. Only pathspecs git knows are used (tracked files,
    or untracked files that are not ignored): a missing, empty or ignored-only folder is skipped. Changes
    outside `pathspecs` (in the working tree or the index) are left as they are. Returns False, and makes no
    commit, when nothing under `pathspecs` changed. Raises GitError when the repo is not on a clean branch."""
    require_on_branch(repo)
    known = [
        p
        for p in dict.fromkeys(pathspecs)
        if git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "--", p).strip()
    ]
    if not known:
        return False
    git(repo, "add", "-A", "--", *known)
    changed = git(repo, "diff", "--cached", "--name-only", "--", *known).splitlines()
    if not changed:
        return False
    log.info("commit %s: %s (%d file(s))", repo.name, message, len(changed))
    _commit(repo, message, known, author=author)
    return True


def ahead_of_upstream(repo: Path) -> bool:
    """True when the branch has commits its upstream lacks. Raises GitError when it has no upstream."""
    try:
        git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    except GitError as e:
        raise GitError(
            f"the branch in {repo} has no upstream to push to: set one with `git push -u origin <branch>`"
        ) from e
    return git(repo, "rev-list", "--count", "@{upstream}..HEAD").strip() != "0"


def push(repo: Path) -> None:
    if not has_remote(repo):
        return
    log.info("push %s", repo.name)
    try:
        git(repo, "push")
    except GitError:
        log.warning("push %s was rejected, retrying once after a rebase", repo.name)
        _pull_rebase(repo)
        git(repo, "push")
