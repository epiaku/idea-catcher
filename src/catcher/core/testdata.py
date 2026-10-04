import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

MARKER = ".catcher-testdata"
# this file is src/catcher/core/testdata.py, so the project root is three levels up
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TARGET = PROJECT_ROOT / "tmp" / "ic"  # tmp/ is in .gitignore
DEFAULT_SOURCE = PROJECT_ROOT / "tests" / "data"
REPOS = ("idea-bucket", "epiaku-docs")
SAVED_CALLS = ("facts", "llm")  # in idea-bucket: the saved YouTube facts and saved LLM replies


class TestDataError(RuntimeError):
    __test__ = False  # not a pytest class


_Ignore = Callable[[str, list[str]], set[str]]


def _top_level_only(root: Path, ignore: _Ignore | None) -> _Ignore | None:
    """Apply `ignore` to the folder `root` itself only, not to a folder of the same name deeper down."""
    if ignore is None:
        return None

    def skip(directory: str, names: list[str]) -> set[str]:
        return set(ignore(directory, names)) if Path(directory) == root else set()

    return skip


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def reset_test_repos(
    target: Path = DEFAULT_TARGET, source: Path = DEFAULT_SOURCE, *, fresh_llm_and_youtube: bool = False
) -> dict[str, Path]:
    """Throw away the test repos in `target` and make fresh ones from the committed test data.

    `source` holds `idea-bucket/` (with `inbox/`) and `epiaku-docs/` (with the Hugo idea-bucket pages), only
    the folders the Idea Catcher reads and writes. Each becomes a git repo with one commit and **no remote**,
    so nothing can be pulled or pushed by mistake.

    The test data holds the saved YouTube facts (`facts/`) and saved LLM replies (`llm/`) of a real run, so
    a run on the copies calls neither. With `fresh_llm_and_youtube` the idea-bucket copy is made without
    those two folders, and a run calls the LLM and YouTube for real (it costs money, the YouTube gap applies).
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
        leave_out = (
            shutil.ignore_patterns(*SAVED_CALLS) if fresh_llm_and_youtube and name == "idea-bucket" else None
        )
        shutil.copytree(source / name, repo, ignore=_top_level_only(source / name, leave_out))
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
