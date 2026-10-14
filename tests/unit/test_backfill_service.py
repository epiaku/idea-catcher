import re

from catcher.modules.backfill.channel import largest_listing
from catcher.modules.backfill.service import listing_fit


def test_listing_fit_none_when_it_fits() -> None:
    assert listing_fit(30, 120, 10) is None


def test_listing_fit_names_the_largest_size() -> None:
    text = listing_fit(100, 120, 20)

    assert text is not None
    match = re.search(r"use at most --max-videos (\d+)", text)
    assert match is not None
    assert int(match.group(1)) == largest_listing(60, 20)
    assert "nothing was done" not in text


def test_listing_fit_says_raise_the_gap_when_not_even_one_page_fits() -> None:
    text = listing_fit(30, 10, 20)

    assert text is not None
    assert "raise the gap" in text
    assert "nothing was done" not in text


def test_listing_fit_names_the_option_it_is_given() -> None:
    text = listing_fit(100, 120, 20, option="max_videos")

    assert text is not None
    assert "max_videos 100 takes" in text
    assert "use at most max_videos 90" in text
    assert "--max-videos" not in text


def test_listing_fit_defaults_to_the_cli_option_name() -> None:
    text = listing_fit(100, 120, 20)

    assert text is not None
    assert "--max-videos 100 takes" in text and "use at most --max-videos 90" in text
