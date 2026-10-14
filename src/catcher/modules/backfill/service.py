"""The backfill steps the CLI and the worker share. No printing and no exits: the callers word the output."""

from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from catcher.modules.backfill import channel, store
from catcher.modules.backfill.release import ReleaseResult, daily_allowance, release
from catcher.modules.backfill.scan import known_ids


def listing_fit(max_videos: int, gap_s: float, delay_s: float) -> str | None:
    """None when a listing of `max_videos` takes at most half the gap its gate slot starts; else why not.
    A longer listing would overlap a worker's fetch or the next listing."""
    if channel.listing_seconds(max_videos, delay_s) <= gap_s / 2:
        return None
    largest = channel.largest_listing(gap_s / 2, delay_s)
    if largest < 1:
        return (
            f"no channel listing fits half of YOUTUBE_MIN_GAP_S={gap_s:g} s with YOUTUBE_REQUEST_DELAY_S="
            f"{delay_s:g} s per page: raise the gap"
        )
    return (
        f"--max-videos {max_videos} takes about {channel.listing_seconds(max_videos, delay_s):g} s, "
        f"more than half of YOUTUBE_MIN_GAP_S={gap_s:g} s: use at most --max-videos {largest} with these "
        "settings"
    )


def store_listing(
    session: Session, ids: list[str], url: str, ideas: Path, docs: Path, now: datetime
) -> tuple[int, int]:
    """Store the listed videos not known yet in the backlog; `(already_known, added)`."""
    known = known_ids(session, ideas, docs)
    fresh = {vid: ("channel", url) for vid in ids if vid not in known}
    added = store.add_pending(session, fresh, now)
    return len(ids) - len(fresh), added


def release_within(session: Session, ideas: Path, cap: int, now: datetime) -> tuple[int, ReleaseResult]:
    """Release what the rolling 24 h `cap` allows; `(allowance, result)`; 0 releases nothing."""
    allowance = daily_allowance(session, cap, now)
    return allowance, release(session, ideas, allowance, now)
