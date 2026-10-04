import math

import pytest

from catcher.modules.youtube.gate_rules import (
    GateState,
    Wait,
    after_block,
    after_reserve,
    after_success,
    clamp,
    closed_state,
    wait_for,
)

NOW = 1_000_000.0
HOUR = 3600.0
OPEN = GateState(next_allowed_at=0.0, blocked_until=0.0, blocked_at=0.0, streak=0)


def test_an_open_state_allows_a_call():
    assert wait_for(OPEN, NOW) is None


def test_reserve_sets_the_gap_with_jitter():
    state = after_reserve(OPEN, NOW, min_gap_s=120, jitter_s=300, rng=lambda: 0.5)
    assert state.next_allowed_at == NOW + 270
    assert (state.blocked_until, state.blocked_at, state.streak) == (0.0, 0.0, 0)


def test_a_closed_gap_returns_the_gap_wait_and_a_block_wins_over_it():
    gap = GateState(next_allowed_at=NOW + 60, blocked_until=0.0, blocked_at=0.0, streak=0)
    assert wait_for(gap, NOW) == Wait(NOW + 60, blocked=False)
    assert wait_for(gap, NOW + 60) is None  # the gap ends at its moment
    both = GateState(next_allowed_at=NOW + 60, blocked_until=NOW + HOUR, blocked_at=NOW, streak=1)
    assert wait_for(both, NOW) == Wait(NOW + HOUR, blocked=True)


def test_blocks_double_up_to_24_hours():
    state, now, lengths = OPEN, NOW, []
    for _ in range(4):
        state = after_block(state, now, None, block_hours=6)
        lengths.append((state.blocked_until - now) / HOUR)
        assert state.blocked_at == now
        now = state.blocked_until + 1
    assert lengths == [6, 12, 24, 24]
    assert state.streak == 4


def test_a_block_newer_than_the_fetch_is_not_doubled():
    started = NOW
    first = after_block(OPEN, NOW, started, block_hours=6)
    second = after_block(first, NOW + 5, started, block_hours=6)  # the other fetch of the same moment
    assert second == first
    older = after_block(first, NOW + 5, started - 1, block_hours=6)  # still newer than that fetch too
    assert older == first
    later = after_block(first, NOW + 5, NOW + 1, block_hours=6)  # a fetch started after that block
    assert later.streak == 2 and later.blocked_until == NOW + 5 + 12 * HOUR


def test_a_success_of_an_older_fetch_does_not_close_a_newer_block():
    blocked = after_block(OPEN, NOW + 5, None, block_hours=6)
    assert after_success(blocked, NOW) == blocked  # that fetch started before the block
    cleared = after_success(blocked, NOW + 5)  # not after: started at the same moment
    assert (cleared.streak, cleared.blocked_until) == (0, 0.0)
    assert cleared.blocked_at == NOW + 5
    assert after_success(blocked, None).streak == 0  # no start known: a success closes the breaker


def test_a_value_beyond_24_hours_is_clamped():
    far = GateState(next_allowed_at=NOW + 10**12, blocked_until=NOW + 10**12, blocked_at=NOW, streak=1)
    state = clamp(far, NOW)
    assert state.blocked_until == NOW + 24 * HOUR
    assert state.next_allowed_at == NOW + 24 * HOUR
    assert (state.blocked_at, state.streak) == (NOW, 1)
    assert clamp(OPEN, NOW) == OPEN


@pytest.mark.parametrize(
    "damage",
    [
        {"next_allowed_at": -5.0},
        {"blocked_until": math.nan},
        {"blocked_at": math.inf},
        {"streak": -1},
    ],
)
def test_a_negative_or_nan_value_is_rejected(damage):
    values = {"next_allowed_at": 0.0, "blocked_until": 0.0, "blocked_at": 0.0, "streak": 0} | damage
    with pytest.raises(ValueError):
        clamp(GateState(**values), NOW)


def test_closed_state_blocks_for_at_least_one_hour():
    assert closed_state(NOW, 6) == GateState(
        next_allowed_at=0.0, blocked_until=NOW + 6 * HOUR, blocked_at=NOW, streak=1
    )
    short = closed_state(NOW, 0)
    assert short.blocked_until == NOW + HOUR
    assert wait_for(short, NOW) == Wait(NOW + HOUR, blocked=True)
