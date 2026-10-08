"""Review Focus 3: nothing in `src` can reach the YouTube fetcher without going through a Gate.

The fetcher `fetch_facts` is used only inside `build_access` (whose `YoutubeAccess` asks the gate before each
fetch), and the pipeline's `Services` are built only in `default_services`, which always sets `youtube`."""

import ast
from pathlib import Path

import pytest

from catcher.core.config import Settings
from catcher.modules.pipeline.process import Services, default_services
from catcher.modules.youtube.facts import FactsUnavailable
from catcher.modules.youtube.pg_gate import PostgresGate

SRC = Path(__file__).parents[2] / "src" / "catcher"


def _uses(name: str, *, calls_only: bool = False) -> list[tuple[str, str]]:
    """(file, enclosing function) of every use of `name` in src (with `calls_only`, of every call to it);
    imports and its own definition are not uses."""
    found: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(SRC).as_posix()

        def visit(node: ast.AST, function: str, rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                inner = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else function
                if calls_only:
                    hit = (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and child.func.id == name
                    )
                else:
                    hit = isinstance(child, ast.Name) and child.id == name
                if hit:
                    found.append((rel, function))
                visit(child, inner, rel)

        visit(tree, "<module>")
    return found


def test_the_fetcher_is_used_only_inside_build_access():
    assert _uses("fetch_facts") == [("modules/youtube/access.py", "fetch")]  # the closure in build_access
    assert _uses("build_access") == [
        ("cli.py", "youtube_facts"),
        ("cli.py", "youtube_import"),  # the channel listing: `YoutubeAccess.call`, through the same gate
        ("modules/pipeline/process.py", "default_services"),
    ]


def test_services_are_built_only_by_default_services_and_always_with_the_gate():
    assert _uses("Services", calls_only=True) == [("modules/pipeline/process.py", "default_services")]
    services = default_services(Settings())  # DATABASE_URL: a closed port (tests/conftest.py); no connection
    assert services.youtube is not None and isinstance(services.youtube.gate, PostgresGate)
    try:
        # `facts` is not a way around the gate: the real services keep the raising default, and every fetch
        # goes through `youtube` (the access, which asks the gate first).
        with pytest.raises(FactsUnavailable, match="only through the gate"):
            services.facts("nGVZS_wUDGM")
    finally:
        services.youtube.gate.engine.dispose()


def test_services_without_an_access_have_no_fetcher_by_default():
    """The `youtube=None` path is for tests only: without a fetcher passed in, nothing can reach YouTube."""
    services = Services(settings=Settings(), profiles=None, backends=None, tags=None)  # type: ignore[arg-type]
    with pytest.raises(FactsUnavailable, match="through the gate"):
        services.facts("nGVZS_wUDGM")


def test_the_channel_listing_is_built_and_called_only_where_the_gate_is_passed():
    """`build_extractor` (yt-dlp) is built only in `_list_channels`, and `list_channel` runs there only inside
    `access.call` (the gate slot, the breaker)."""
    assert _named("build_extractor") == [("cli.py", "_list_channels")]
    assert _named("list_channel") == [("cli.py", "_list_channels")]
    assert _named("_list_channels") == [("cli.py", "youtube_import")]


def _named(name: str) -> list[tuple[str, str]]:
    """(file, enclosing function) of every use of `name` in src, as a name or as `module.name` (the CLI
    reaches the listing through its module, so a test can replace the extractor)."""
    found: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == "modules/backfill/channel.py":
            continue  # the definitions themselves
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(function):
                if (isinstance(node, ast.Name) and node.id == name) or (
                    isinstance(node, ast.Attribute) and node.attr == name
                ):
                    found.append((rel, function.name))
    return sorted(set(found))


def test_only_the_fetcher_and_the_channel_listing_import_yt_dlp():
    """A direct yt-dlp call anywhere else in src would bypass the gate: only these two modules import it."""
    importers: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(name == "yt_dlp" or name.startswith("yt_dlp.") for name in names):
                importers.append(path.relative_to(SRC).as_posix())
    allowed = ["modules/backfill/channel.py", "modules/youtube/facts.py"]
    assert sorted(set(importers)) == allowed
    # no way around the import statement either (importlib, __import__, a string): the name itself
    mentions = [
        p.relative_to(SRC).as_posix() for p in sorted(SRC.rglob("*.py")) if "yt_dlp" in p.read_text("utf-8")
    ]
    assert mentions == allowed


def test_the_listing_runs_only_inside_access_call():
    """In cli.py, `list_channel` is called only inside the callable handed to `access.call` (the gate)."""
    tree = ast.parse((SRC / "cli.py").read_text(encoding="utf-8"))
    inside: list[ast.AST] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "call"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "access"
        ):
            inside.extend(ast.walk(node.args[0]))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "list_channel"
    ]
    assert calls and all(any(c is n for n in inside) for c in calls)
