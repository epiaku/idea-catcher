import json
import threading

import pytest

from catcher.modules.youtube.gate import MAX_BLOCK_HOURS, Wait, YoutubeGate, is_block_error


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_gate(tmp_path, clock, *, jitter=0.0, rng=lambda: 0.5, gap=600.0, hours=6.0) -> YoutubeGate:
    return YoutubeGate(tmp_path, min_gap_s=gap, jitter_s=jitter, block_hours=hours, clock=clock, rng=rng)


def test_the_first_call_is_allowed_and_starts_the_gap(tmp_path):
    clock = Clock()
    gate = make_gate(tmp_path, clock)
    assert gate.reserve() is None
    wait = gate.reserve()
    assert wait == Wait(clock.now + 600, blocked=False)  # 10 minutes after the START of the first


def test_after_the_gap_a_call_is_allowed_again(tmp_path):
    clock = Clock()
    gate = make_gate(tmp_path, clock)
    gate.reserve()
    clock.now += 599
    assert gate.peek() is not None
    clock.now += 2
    assert gate.peek() is None and gate.reserve() is None


def test_peek_changes_nothing(tmp_path):
    clock = Clock()
    gate = make_gate(tmp_path, clock)
    for _ in range(3):
        assert gate.peek() is None
    assert gate.reserve() is None  # still the first call


def test_the_jitter_adds_random_seconds_within_its_limit(tmp_path):
    clock = Clock()
    gate = make_gate(tmp_path, clock, jitter=300, rng=lambda: 1.0)
    gate.reserve()
    assert gate.peek() == Wait(clock.now + 900, blocked=False)
    other = make_gate(tmp_path / "b", clock, jitter=300, rng=lambda: 0.0)
    other.reserve()
    assert other.peek() == Wait(clock.now + 600, blocked=False)


def test_a_block_stops_all_calls_for_6_hours_then_12_then_24(tmp_path):
    clock = Clock()
    gate = make_gate(tmp_path, clock, gap=0)
    until = gate.record_block()
    assert until == clock.now + 6 * 3600
    assert gate.reserve() == Wait(until, blocked=True)
    clock.now = until + 1  # the probe: allowed again, and it is blocked again
    assert gate.reserve() is None
    assert gate.record_block() == clock.now + 12 * 3600
    clock.now += 12 * 3600 + 1
    assert gate.record_block() == clock.now + 24 * 3600
    clock.now += 24 * 3600 + 1
    assert gate.record_block() == clock.now + MAX_BLOCK_HOURS * 3600  # it stops at 24 hours


def test_a_success_closes_the_breaker_and_resets_the_streak(tmp_path):
    clock = Clock()
    gate = make_gate(tmp_path, clock, gap=0)
    gate.record_block()
    clock.now += 7 * 3600
    gate.record_success()
    assert gate.peek() is None
    assert gate.record_block() == clock.now + 6 * 3600  # back to 6 hours, not 12


def test_the_state_is_shared_through_the_file(tmp_path):
    clock = Clock()
    first, second = make_gate(tmp_path, clock), make_gate(tmp_path, clock)  # two runs on one machine
    assert first.reserve() is None
    assert second.reserve() is not None
    first.record_block()
    assert second.peek().blocked  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"blocked_until": "soon"}', '{"blocked_until": NaN}', '{"next_allowed_at": -5}'],
)
def test_a_damaged_state_file_closes_the_gate_and_is_kept(tmp_path, content):
    """Losing an active 24 hour block to a damaged file is the expensive mistake: fail closed."""
    clock = Clock()
    gate = make_gate(tmp_path, clock)
    gate.state_file.parent.mkdir(parents=True, exist_ok=True)
    gate.state_file.write_text(content)
    wait = gate.reserve()
    assert wait is not None and wait.blocked
    assert wait.until == clock.now + 6 * 3600  # block_hours of the test gate
    assert gate.state_file.with_suffix(".corrupt").read_text() == content  # kept for a look
    assert gate.reserve() is not None  # and it stays closed on the next call


def test_a_block_from_the_far_future_is_capped_at_24_hours(tmp_path):
    clock = Clock()
    gate = make_gate(tmp_path, clock)
    gate.state_file.parent.mkdir(parents=True, exist_ok=True)
    gate.state_file.write_text(json.dumps({"blocked_until": clock.now + 10**12}))
    wait = gate.peek()
    assert wait is not None and wait.until == clock.now + 24 * 3600


def test_a_success_that_started_before_a_newer_block_does_not_close_the_breaker(tmp_path):
    clock = Clock()
    gate = make_gate(tmp_path, clock)
    started = clock.now  # fetch A is reserved ...
    clock.now += 5
    gate.record_block()  # ... fetch B gets a 429 while A is still running ...
    gate.record_success(started)  # ... and A then works: B's block stays
    assert gate.peek() is not None and gate.peek().blocked  # type: ignore[union-attr]


def test_two_fetches_that_both_get_a_block_are_one_block_not_two(tmp_path):
    clock = Clock()
    gate = make_gate(tmp_path, clock)
    started = clock.now
    first = gate.record_block(started)
    clock.now += 5
    second = gate.record_block(started)  # the other fetch, started at the same time, gets its 429 too
    assert second == first  # not doubled to 12 hours
    assert json.loads(gate.state_file.read_text())["streak"] == 1


def test_two_threads_asking_at_once_cannot_both_go(tmp_path):
    clock = Clock()
    results: list[object] = []

    def ask() -> None:
        results.append(make_gate(tmp_path, clock).reserve())

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(None) == 1  # the lock lets exactly one through


def test_the_state_file_is_plain_json_on_this_machine(tmp_path):
    gate = make_gate(tmp_path, Clock())
    gate.reserve()
    data = json.loads(gate.state_file.read_text())
    assert set(data) == {"next_allowed_at", "blocked_until", "blocked_at", "streak"}


def test_the_wait_message_says_what_and_when(tmp_path):
    clock = Clock()
    gap = Wait(clock.now + 600, blocked=False).message(clock.now)
    block = Wait(clock.now + 3600, blocked=True).message(clock.now)
    assert gap.startswith("YouTube: next call allowed at ")
    assert block.startswith("YouTube blocked until ")


class FakeHttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP Error {status}")
        self.status = status


@pytest.mark.parametrize(
    "error",
    [
        FakeHttpError(429),
        RuntimeError("ERROR: [youtube] x: Sign in to confirm you’re not a bot. Use --cookies"),
        RuntimeError("Sign in to confirm you're not a bot"),
        RuntimeError("YouTube is blocking requests from your IP"),
        RuntimeError("HTTP Error 429: Too Many Requests"),
    ],
)
def test_a_429_or_a_bot_check_is_a_block(error):
    assert is_block_error(error)


def test_the_block_is_found_inside_a_wrapped_error():
    try:
        try:
            raise FakeHttpError(429)
        except FakeHttpError as inner:
            raise RuntimeError("yt-dlp failed for abc") from inner
    except RuntimeError as outer:
        assert is_block_error(outer)


@pytest.mark.parametrize(
    "error",
    [FakeHttpError(404), FakeHttpError(500), RuntimeError("Video unavailable"), ConnectionError("no route")],
)
def test_other_errors_do_not_open_the_breaker(error):
    assert not is_block_error(error)
