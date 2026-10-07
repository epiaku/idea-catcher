from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from catcher.core.config import Settings
from catcher.modules.scheduler.schedule import SCHEDULE_NAMES, ScheduleSpec, due_slot, parse_schedules

AMS = ZoneInfo("Europe/Amsterdam")


def settings(**kw) -> Settings:
    base = {"schedule_ideas_pull": "", "schedule_pipeline_run": "", "schedule_publish": ""}
    return Settings(_env_file=None, **{**base, **kw})


def spec(cron: str) -> ScheduleSpec:
    return ScheduleSpec("x", cron, "pipeline.run", {}, "SCHEDULE_PIPELINE_RUN")


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


def test_empty_variables_mean_no_schedule():
    assert parse_schedules(settings()) == []
    assert SCHEDULE_NAMES == ("ideas_pull", "pipeline_run", "publish")


@pytest.mark.parametrize("cron", ["0 8 * * * *", "every day", "61 * * * *", "* * * *"])
def test_a_bad_cron_names_the_variable(cron):
    with pytest.raises(ValueError, match="SCHEDULE_PUBLISH"):
        parse_schedules(settings(schedule_publish=cron))


def test_an_unknown_timezone_is_refused():
    with pytest.raises(ValueError, match="SCHEDULE_TIMEZONE"):
        parse_schedules(settings(schedule_timezone="Mars/Olympus", schedule_publish="0 8 * * *"))


def test_the_next_slot_is_due_once_it_has_passed():
    s = spec("0 8 * * *")  # 08:00 Amsterdam in winter = 07:00 UTC
    slot = due_slot(s, AMS, utc(2026, 1, 10, 6, 0), utc(2026, 1, 10, 7, 0, 5))
    assert slot == utc(2026, 1, 10, 7, 0)
    assert slot.tzinfo is not None and slot.utcoffset() == timedelta(0)


def test_three_days_of_downtime_give_one_slot_the_latest():
    slot = due_slot(spec("0 8 * * *"), AMS, utc(2026, 1, 10, 6, 0), utc(2026, 1, 13, 12, 0))
    assert slot == utc(2026, 1, 13, 7, 0)


def test_nothing_is_due_before_the_slot():
    assert due_slot(spec("0 8 * * *"), AMS, utc(2026, 1, 10, 6, 0), utc(2026, 1, 10, 6, 59)) is None


def test_nothing_is_due_when_last_fired_is_in_the_future():
    assert due_slot(spec("* * * * *"), AMS, utc(2026, 1, 10, 9, 0), utc(2026, 1, 10, 8, 0)) is None


def sweep(day_start_utc: datetime, days: int = 1) -> dict:
    """Minute by minute, last_fired_at following each returned slot; fires counted per Amsterdam day."""
    s = spec("30 2 * * *")
    last, now, end = day_start_utc, day_start_utc, day_start_utc + timedelta(days=days)
    fires: dict = {}
    while now <= end:
        slot = due_slot(s, AMS, last, now)
        if slot is not None:
            last = slot
            day = slot.astimezone(AMS).date().isoformat()
            fires[day] = fires.get(day, 0) + 1
        now += timedelta(minutes=1)
    return fires


def test_the_spring_forward_gap_fires_once():
    """2026-03-29: 02:30 does not exist in Amsterdam (02:00 jumps to 03:00). Whatever croniter does with the
    nonexistent slot (it shifts it to 03:30 CEST or 01:30 UTC-equivalent), the sweep must show exactly one
    fire on that local day, and one on the days around it."""
    fires = sweep(utc(2026, 3, 27, 12, 0), days=4)
    assert fires["2026-03-29"] == 1
    assert fires["2026-03-28"] == 1 and fires["2026-03-30"] == 1


def test_the_fall_back_hour_fires_once():
    """2026-10-25: 02:30 happens twice (CEST then CET). It must fire once, not twice."""
    fires = sweep(utc(2026, 10, 23, 12, 0), days=4)
    assert fires["2026-10-25"] == 1
    assert fires["2026-10-24"] == 1 and fires["2026-10-26"] == 1


def test_the_job_types_and_params_of_the_three_schedules():
    specs = parse_schedules(
        settings(
            schedule_ideas_pull="*/30 * * * *",
            schedule_pipeline_run="0 7 * * *",
            schedule_publish="0 6 * * *",
        )
    )
    got = {s.name: (s.job_type, s.params, s.variable, s.cron) for s in specs}
    assert got == {
        "ideas_pull": ("ideas.pull", {}, "SCHEDULE_IDEAS_PULL", "*/30 * * * *"),
        "pipeline_run": ("pipeline.run", {"retry_deferred": True}, "SCHEDULE_PIPELINE_RUN", "0 7 * * *"),
        "publish": ("pipeline.publish", {"pull": True, "push": True}, "SCHEDULE_PUBLISH", "0 6 * * *"),
    }
