"""B5b: `catcher run pipeline` is the worker path. The old Stage A loop (`run_pipeline`, `RunOptions`, the
`RunLockLost` plumbing and its `lock_check` option) is deleted; the helpers the handlers use live on."""

import importlib
from pathlib import Path

import catcher

SRC = Path(catcher.__file__).parent
GONE = ("def run_pipeline(", "class RunOptions", "RunLockLost", "pipeline.run import")


def test_the_old_loop_is_gone() -> None:
    found = [
        f"{path.relative_to(SRC)}: {needle}"
        for path in sorted(SRC.rglob("*.py"))
        for needle in GONE
        if needle in path.read_text(encoding="utf-8")
    ]
    assert found == []
    assert not (SRC / "modules" / "pipeline" / "run.py").exists()


def test_only_the_worker_takes_a_lock_check() -> None:
    users = sorted(
        str(path.relative_to(SRC)) for path in SRC.rglob("*.py") if "lock_check" in path.read_text("utf-8")
    )
    assert users == ["cli.py", "modules/worker/loop.py", "modules/worker/runner.py"]


def test_the_handlers_still_import_their_helpers() -> None:
    handlers = importlib.import_module("catcher.modules.worker.handlers_pipeline")
    for name in ("apply_outcome", "log_outcome", "RunState", "copy_artifacts", "write_output"):
        assert callable(getattr(handlers, name)), name
