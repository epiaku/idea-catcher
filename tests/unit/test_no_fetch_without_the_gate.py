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
