import logging
import subprocess
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path

log = logging.getLogger("catcher.git")

# One git writer per process: the worker's publish holds it around pull, commit and push.
GIT_LOCK = threading.Lock()


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if out.returncode != 0:
        message = f"git {' '.join(args[:3])} failed in {repo}: {out.stderr.strip()[:500]}"
        log.error(message)
        raise GitError(message)
    return out.stdout


def has_remote(repo: Path) -> bool:
    return bool(git(repo, "remote").strip())


def pull(repo: Path) -> None:
    if has_remote(repo):
        log.info("pull %s", repo.name)
        git(repo, "pull", "--rebase", "--autostash")


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


def commit_managed(repo: Path, pathspecs: Sequence[str], message: str, *, author: tuple[str, str]) -> bool:
    """Commit everything under `pathspecs` (folders relative to `repo`): new, changed and deleted files, so a
    note moved out of `inbox/` is committed as a deletion. A pathspec that is neither on disk nor tracked is
    skipped. Changes outside `pathspecs` (in the working tree or the index) are left as they are. Returns
    False, and makes no commit, when nothing under `pathspecs` changed."""
    present = [p for p in dict.fromkeys(pathspecs) if (repo / p).exists() or git(repo, "ls-files", "--", p)]
    if not present:
        return False
    git(repo, "add", "-A", "--", *present)
    changed = git(repo, "diff", "--cached", "--name-only", "--", *present).splitlines()
    if not changed:
        return False
    log.info("commit %s: %s (%d file(s))", repo.name, message, len(changed))
    _commit(repo, message, present, author=author)
    return True


def ahead_of_upstream(repo: Path) -> bool:
    """True when the branch has commits its upstream lacks, or has no upstream to compare with."""
    try:
        return git(repo, "rev-list", "--count", "@{upstream}..HEAD").strip() != "0"
    except GitError:
        return True


def push(repo: Path) -> None:
    if not has_remote(repo):
        return
    log.info("push %s", repo.name)
    try:
        git(repo, "push")
    except GitError:
        log.warning("push %s was rejected, retrying once after a rebase", repo.name)
        git(repo, "pull", "--rebase", "--autostash")
        git(repo, "push")
