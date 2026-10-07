"""The container entrypoint and the git askpass script, run with real git and local bare repos."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _clean_env() -> dict[str, str]:
    """The environment without GIT_* variables (a git hook sets GIT_INDEX_FILE, which breaks other repos)."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


ENTRYPOINT = ROOT / "scripts" / "docker-entrypoint.sh"
ASKPASS = ROOT / "scripts" / "git-askpass.sh"


def _git(*args: str, cwd: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    result = subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)
    return result.stdout


def _bare_remote(base: Path, name: str) -> Path:
    work = base / f"{name}-work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "README.md").write_text("hello\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "first", cwd=work)
    bare = base / f"{name}.git"
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=base)
    return bare


def _run(tmp_path: Path, args: list[str], **extra: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("IDEAS_", "DOCS_", "GITHUB_"))}
    env["HOME"] = str(tmp_path / "home")
    env.update(extra)
    return subprocess.run(
        ["bash", str(ENTRYPOINT), *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )


def _both(tmp_path: Path, ideas_remote: str = "", docs_remote: str = "") -> dict[str, str]:
    env = {"IDEAS_REPO": str(tmp_path / "ideas"), "DOCS_REPO": str(tmp_path / "docs")}
    if ideas_remote:
        env["IDEAS_REMOTE"] = ideas_remote
    if docs_remote:
        env["DOCS_REMOTE"] = docs_remote
    return env


def test_a_missing_repo_is_cloned_from_its_remote(tmp_path: Path) -> None:
    ideas = _bare_remote(tmp_path, "ri")
    docs = _bare_remote(tmp_path, "rd")
    result = _run(tmp_path, ["true"], **_both(tmp_path, str(ideas), str(docs)))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ideas" / "README.md").read_text() == "hello\n"
    assert (tmp_path / "docs" / ".git").exists()


def test_an_existing_clone_is_left_alone(tmp_path: Path) -> None:
    ideas = _bare_remote(tmp_path, "ri")
    docs = _bare_remote(tmp_path, "rd")
    _git("clone", "-q", str(ideas), str(tmp_path / "ideas"), cwd=tmp_path)
    _git("clone", "-q", str(docs), str(tmp_path / "docs"), cwd=tmp_path)
    (tmp_path / "ideas" / "local.txt").write_text("x")
    _git("add", ".", cwd=tmp_path / "ideas")
    _git("commit", "-q", "-m", "local", cwd=tmp_path / "ideas")
    # A remote that would fail proves no clone or fetch is tried.
    result = _run(
        tmp_path, ["true"], **_both(tmp_path, "https://127.0.0.1:1/none.git", "https://127.0.0.1:1/none.git")
    )
    assert result.returncode == 0, result.stderr
    assert "local" in _git("log", "--format=%s", cwd=tmp_path / "ideas")


def test_a_directory_without_git_is_never_touched(tmp_path: Path) -> None:
    ideas = _bare_remote(tmp_path, "ri")
    docs = _bare_remote(tmp_path, "rd")
    (tmp_path / "ideas").mkdir()
    (tmp_path / "ideas" / "keep.txt").write_text("mine")
    result = _run(tmp_path, ["true"], **_both(tmp_path, str(ideas), str(docs)))
    assert result.returncode == 2
    assert "IDEAS_REMOTE" in result.stderr
    assert (tmp_path / "ideas" / "keep.txt").read_text() == "mine"
    assert not (tmp_path / "ideas" / ".git").exists()


def test_a_missing_repo_without_a_remote_exits_2_naming_the_variable(tmp_path: Path) -> None:
    docs = _bare_remote(tmp_path, "rd")
    result = _run(tmp_path, ["true"], **_both(tmp_path, "", str(docs)))
    assert result.returncode == 2
    assert "IDEAS_REMOTE" in result.stderr
    assert not (tmp_path / "ideas").exists()
    result = _run(tmp_path, ["true"], **_both(tmp_path, str(docs), ""))
    assert result.returncode == 2
    assert "DOCS_REMOTE" in result.stderr


def test_a_failed_clone_removes_only_the_empty_directory_it_made_and_hides_credentials(
    tmp_path: Path,
) -> None:
    docs = _bare_remote(tmp_path, "rd")
    sibling = tmp_path / "docs"
    sibling.mkdir()
    (sibling / "precious.txt").write_text("keep")
    # Not a git checkout and non-empty: exit 2 without touching it. Use a separate failing ideas repo first.
    bad = "https://user:secret@127.0.0.1:1/x.git"
    result = _run(tmp_path, ["true"], **_both(tmp_path, bad, str(docs)))
    assert result.returncode != 0
    assert "secret" not in result.stdout + result.stderr
    assert "IDEAS_REMOTE" in result.stderr
    assert not (tmp_path / "ideas").exists()
    assert (sibling / "precious.txt").read_text() == "keep"


def test_a_failed_clone_keeps_a_directory_that_was_there_before(tmp_path: Path) -> None:
    (tmp_path / "ideas").mkdir()
    docs = _bare_remote(tmp_path, "rd")
    bad = "https://user:secret@127.0.0.1:1/x.git"
    result = _run(tmp_path, ["true"], **_both(tmp_path, bad, str(docs)))
    assert result.returncode != 0
    assert "secret" not in result.stdout + result.stderr
    assert (tmp_path / "ideas").is_dir()


def test_the_command_is_exec_ed_with_its_arguments(tmp_path: Path) -> None:
    ideas = _bare_remote(tmp_path, "ri")
    docs = _bare_remote(tmp_path, "rd")
    script = 'echo "$1-$2"; exit 7'
    result = _run(tmp_path, ["sh", "-c", script, "sh", "a", "b"], **_both(tmp_path, str(ideas), str(docs)))
    assert result.returncode == 7
    assert result.stdout.strip().endswith("a-b")
    pid = _run(tmp_path, ["sh", "-c", "echo $$"], **_both(tmp_path, str(ideas), str(docs)))
    assert pid.stdout.strip().isdigit()


def test_the_scripts_are_executable_in_git() -> None:
    out = subprocess.run(
        ["git", "ls-files", "-s", "scripts/docker-entrypoint.sh", "scripts/git-askpass.sh"],
        cwd=ROOT,
        env=_clean_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert len(out) == 2
    assert all(line.startswith("100755") for line in out), out


def _askpass(prompt: str, token: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
    if token is not None:
        env["GITHUB_TOKEN"] = token
    return subprocess.run(["sh", str(ASKPASS), prompt], env=env, capture_output=True, text=True, timeout=20)


def test_askpass_answers_username_and_token_and_never_prints_the_token_to_stderr() -> None:
    user = _askpass("Username for 'https://github.com': ", "tok-123")
    assert user.stdout.strip() == "x-access-token"
    password = _askpass("Password for 'https://x-access-token@github.com': ", "tok-123")
    assert password.returncode == 0
    assert password.stdout.strip() == "tok-123"
    assert "tok-123" not in user.stderr + password.stderr


def test_askpass_without_a_token_fails() -> None:
    for token in (None, ""):
        result = _askpass("Password for 'https://github.com': ", token)
        assert result.returncode == 1
        assert result.stdout == ""
