"""The saved facts of YouTube videos: one file per video id, in `facts/` of the idea-bucket repo.

With it a video is fetched from YouTube **once**: a retry, a requeue or a rerun reads the saved file. The file
is committed with everything else, so it is backed up and it survives a new machine.
"""

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from catcher.core.files import write_atomic
from catcher.modules.youtube.facts import YoutubeFacts

log = logging.getLogger("catcher.youtube")

FACTS_DIR = "facts"
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class FactsCache:
    def __init__(
        self,
        directory: Path,
        *,
        negative_ttl_s: float = 24 * 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory = directory
        self.negative_ttl_s = negative_ttl_s
        self.clock = clock

    def path(self, video_id: str) -> Path:
        if not _VIDEO_ID.match(video_id):
            raise ValueError(f"not a YouTube video id: {video_id!r}")
        return self.directory / f"{video_id}.json"

    def get(self, video_id: str) -> YoutubeFacts | None:
        """The saved facts, or None when there are none (or they are unreadable, or too old to trust).

        A video without a transcript is remembered only for `negative_ttl_s`: captions can appear later, but
        asking again at every run is exactly the hammering we want to avoid.
        """
        path = self.path(video_id)
        try:
            facts = YoutubeFacts.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValidationError, OSError, ValueError) as e:
            log.warning("ignoring unreadable saved facts %s: %s", path.name, e)
            return None
        if not facts.transcript and not self._fresh(facts):  # also a video that is gone for good
            return None
        return facts

    def _fresh(self, facts: YoutubeFacts) -> bool:
        if not facts.fetched_utc:
            return False
        try:
            fetched = datetime.fromisoformat(facts.fetched_utc).timestamp()
        except ValueError:
            return False
        return self.clock() - fetched < self.negative_ttl_s

    def put(self, facts: YoutubeFacts) -> Path:
        path = self.path(facts.video_id)
        write_atomic(path, facts.model_dump_json(indent=2))  # never a half-written file
        return path
