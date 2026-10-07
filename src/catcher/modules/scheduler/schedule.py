"""The pure rules of the scheduler: which schedules are on, and which slot is due. No database, no threads."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from catcher.core.config import Settings

SCHEDULE_NAMES = ("ideas_pull", "pipeline_run", "publish")

# name -> (job type, params); the settings field and the variable name follow from the name
_JOBS: dict[str, tuple[str, dict[str, Any]]] = {
    "ideas_pull": ("ideas.pull", {}),
    "pipeline_run": ("pipeline.run", {"retry_deferred": True}),
    "publish": ("pipeline.publish", {"pull": True, "push": True}),
}


@dataclass(frozen=True)
class ScheduleSpec:
    name: str
    cron: str
    job_type: str
    params: dict[str, Any]
    variable: str


def load_timezone(settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo(settings.schedule_timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"SCHEDULE_TIMEZONE: unknown timezone {settings.schedule_timezone!r}") from exc


def check_cron(variable: str, cron: str) -> None:
    # croniter also accepts six fields (seconds); the schedule is minute-based, so only five are allowed
    if len(cron.split()) != 5 or not croniter.is_valid(cron):
        raise ValueError(f"{variable}: {cron!r} is not a valid cron string (five fields)")


def parse_schedules(settings: Settings) -> list[ScheduleSpec]:
    load_timezone(settings)
    specs = []
    for name in SCHEDULE_NAMES:
        variable = f"SCHEDULE_{name.upper()}"
        cron = getattr(settings, f"schedule_{name}").strip()
        if not cron:
            continue
        check_cron(variable, cron)
        job_type, params = _JOBS[name]
        specs.append(ScheduleSpec(name, cron, job_type, dict(params), variable))
    return specs


def due_slot(spec: ScheduleSpec, tz: ZoneInfo, last_fired_at: datetime, now: datetime) -> datetime | None:
    """The latest slot s with last_fired_at < s <= now (aware UTC in and out; slots in `tz`), or None."""
    if last_fired_at >= now:
        return None
    local_now = now.astimezone(tz)
    # get_prev is strictly before its start; start a moment later so a slot exactly at `now` counts
    slot = croniter(spec.cron, local_now + timedelta(seconds=1)).get_prev(datetime)
    # A wall-clock time that happens twice (fall back) counts once: the second pass maps to the first
    slot_utc = slot.replace(tzinfo=None).replace(tzinfo=tz, fold=0).astimezone(UTC)
    if last_fired_at < slot_utc <= now:
        return slot_utc
    return None
