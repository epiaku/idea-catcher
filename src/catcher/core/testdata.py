import shutil
import subprocess
from pathlib import Path

MARKER = ".catcher-testdata"
# this file is src/catcher/core/testdata.py, so the project root is three levels up
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TARGET = PROJECT_ROOT / "tmp" / "ic"  # tmp/ is in .gitignore
DEFAULT_SOURCE = PROJECT_ROOT / "tests" / "data"
REPOS = ("idea-bucket", "epiaku-docs")


class TestDataError(RuntimeError):
    __test__ = False  # not a pytest class


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def reset_test_repos(target: Path = DEFAULT_TARGET, source: Path = DEFAULT_SOURCE) -> dict[str, Path]:
    """Throw away the test repos in `target` and make fresh ones from the committed test data.

    `source` holds `idea-bucket/` (with `inbox/`) and `epiaku-docs/` (with the Hugo idea-bucket pages), only
    the folders the Idea Catcher reads and writes. Each becomes a git repo with one commit and **no remote**,
    so nothing can be pulled or pushed by mistake.
    """
    for name in REPOS:
        if not (source / name).is_dir():
            raise TestDataError(f"test data not found: {source / name}")
    if target.exists():
        if not (target / MARKER).exists():
            raise TestDataError(
                f"{target} exists and was not made by this command, so it is not deleted. "
                f"Remove it yourself first: rm -rf {target}"
            )
        shutil.rmtree(target)
    repos: dict[str, Path] = {}
    for name in REPOS:
        repo = target / name
        shutil.copytree(source / name, repo)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", "-A")
        _git(
            repo,
            "-c",
            "user.name=test-data",
            "-c",
            "user.email=test-data@example.com",
            "commit",
            "-qm",
            "test data",
        )
        repos[name] = repo
    (target / MARKER).write_text(
        "made by `catcher testdata reset`; safe to delete and recreate\n", encoding="utf-8"
    )
    return repos
