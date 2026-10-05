"""Which errors from YouTube are a block (they open the breaker) and which are ordinary failures."""

import pytest

from catcher.modules.youtube.gate import is_block_error


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
