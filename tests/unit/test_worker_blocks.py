"""`BackendBlocks`: the worker's memory of LLM backends not to call for a while."""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from catcher.modules.worker.blocks import BackendBlocks

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def test_a_block_is_active_until_its_time_and_then_dropped():
    blocks = BackendBlocks()
    blocks.block("openai", NOW + timedelta(seconds=600), "budget reached (openai)")
    assert blocks.active(NOW) == frozenset({"openai"})
    assert blocks.active(NOW + timedelta(seconds=599)) == frozenset({"openai"})
    assert blocks.active(NOW + timedelta(seconds=600)) == frozenset()
    assert blocks.entries(NOW) == {}  # the expired entry is gone for good


def test_entries_give_the_time_and_the_cause():
    blocks = BackendBlocks()
    until = NOW + timedelta(seconds=60)
    blocks.block("freellmapi", until, "connection refused")
    [(backend, entry)] = blocks.entries(NOW).items()
    assert (backend, entry.until, entry.cause) == ("freellmapi", until, "connection refused")


def test_a_new_block_replaces_the_old_one_of_that_backend_only():
    blocks = BackendBlocks()
    blocks.block("openai", NOW + timedelta(seconds=10), "first")
    blocks.block("freellmapi", NOW + timedelta(seconds=10), "other")
    blocks.block("openai", NOW + timedelta(seconds=100), "again")
    assert blocks.active(NOW + timedelta(seconds=50)) == frozenset({"openai"})
    assert blocks.entries(NOW)["openai"].cause == "again"


def test_an_empty_memory_blocks_nothing():
    assert BackendBlocks().active(NOW) == frozenset()


@pytest.mark.parametrize("call", ["block", "active", "entries"])
def test_a_naive_time_is_refused(call):
    blocks = BackendBlocks()
    naive = datetime(2026, 10, 4, 12, 0)
    with pytest.raises(ValueError, match="naive"):
        if call == "block":
            blocks.block("openai", naive, "x")
        elif call == "active":
            blocks.active(naive)
        else:
            blocks.entries(naive)


def test_many_threads_blocking_and_reading_at_once():
    blocks = BackendBlocks()
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(200):
                blocks.block(f"b{n}", NOW + timedelta(seconds=1 + i % 3), "x")
                blocks.active(NOW)
                blocks.entries(NOW)
                blocks.block(f"gone{n}", NOW, "ends at once")  # dropped by the next read
        except BaseException as e:  # pragma: no cover - only on a bug
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert blocks.active(NOW) == frozenset(f"b{n}" for n in range(8))


def test_each_handler_context_gets_its_own_memory():
    from pathlib import Path

    from catcher.core.config import Settings
    from catcher.modules.worker.handlers import HandlerContext

    def make() -> HandlerContext:
        return HandlerContext(
            settings=Settings(),
            services=None,
            engine=None,
            ideas=Path("."),
            docs=Path("."),
            clock=lambda: NOW,
        )  # type: ignore[arg-type]

    one, two = make(), make()
    one.backend_blocks.block("openai", NOW + timedelta(seconds=60), "x")
    assert two.backend_blocks.active(NOW) == frozenset()
