import subprocess
from collections.abc import Iterable
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if out.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed in {repo}: {out.stderr.strip()[:500]}")
    return out.stdout


def has_remote(repo: Path) -> bool:
    return bool(git(repo, "remote").strip())


def pull(repo: Path) -> None:
    if has_remote(repo):
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
        *in_index,
    )
    return True


def push(repo: Path) -> None:
    if not has_remote(repo):
        return
    try:
        git(repo, "push")
    except GitError:
        git(repo, "pull", "--rebase", "--autostash")
        git(repo, "push")
