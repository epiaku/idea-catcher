"""The scheduler: on every tick, queue the job of each schedule whose slot is due. It only enqueues; the
worker does the work. One transaction per schedule holds its `schedules` row lock, so two schedulers on one
database cannot both fire a slot."""

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catcher.core.db import require_aware, session_scope
from catcher.modules.queue.models import Schedule
from catcher.modules.queue.queue import enqueue
from catcher.modules.scheduler.schedule import ScheduleSpec, due_slot

log = logging.getLogger("catcher.scheduler")


class Scheduler:
    def __init__(
        self, engine: Engine, specs: Sequence[ScheduleSpec], tz: ZoneInfo, clock: Callable[[], datetime]
    ) -> None:
        self._engine = engine
        self._specs = list(specs)
        self._tz = tz
        self._clock = clock
        self._failing: set[str] = set()  # schedules whose last tick failed: their error is logged once

    def tick(self) -> list[str]:
        """Handle every schedule once; returns the names that queued a job. An error in one schedule rolls
        back only its own transaction (its slot stays due) and is logged once per failure streak."""
        queued = []
        for spec in self._specs:
            try:
                with session_scope(self._engine) as session:
                    created = self._tick_one(session, spec)
            except Exception:
                if spec.name not in self._failing:
                    log.exception(
                        "schedule %s: the tick failed; it is tried again on the next tick", spec.name
                    )
                    self._failing.add(spec.name)
                continue
            if spec.name in self._failing:
                log.info("schedule %s: works again", spec.name)
                self._failing.discard(spec.name)
            if created:
                queued.append(spec.name)
        return queued

    def _tick_one(self, session: Session, spec: ScheduleSpec) -> bool:
        now = require_aware(self._clock())
        # Create a missing row; a concurrent creator's uncommitted row makes this wait, then do nothing
        inserted = session.execute(
            insert(Schedule)
            .values(name=spec.name, last_fired_at=now)
            .on_conflict_do_nothing(index_elements=[Schedule.name])
            .returning(Schedule.name)
        ).first()
        if inserted is not None:
            log.info("schedule %s: new, starts now; the next slot fires", spec.name)
            return False  # decision 2: a new schedule fires nothing
        row = session.scalars(
            select(Schedule)
            .where(Schedule.name == spec.name)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if row.last_fired_at is None:
            row.last_fired_at = now
            return False
        slot = due_slot(spec, self._tz, row.last_fired_at, now)
        if slot is None:
            return False
        job, created = enqueue(
            session, type=spec.job_type, now=now, params=dict(spec.params), dedupe_key=f"schedule:{spec.name}"
        )
        # The slot is consumed also when the previous job is still active: a missed slot runs once, not later
        row.last_fired_at = now
        if created:
            log.info(
                "schedule %s: slot %s, queued %s job %s", spec.name, slot.isoformat(), spec.job_type, job.id
            )
        else:
            log.info(
                "schedule %s: slot %s, job %s is still %s, no second job",
                spec.name,
                slot.isoformat(),
                job.id,
                job.status,
            )
        return created

    def run_forever(self, stop: threading.Event, tick_s: float) -> None:
        """Tick, then wait `tick_s` or until `stop` is set. A failing tick is logged once per failure streak
        and never ends the loop; only `stop` does."""
        failing = False
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                if not failing:
                    log.exception("scheduler: the tick failed; trying again every %s s", tick_s)
                    failing = True
            else:
                if failing:
                    log.info("scheduler: the tick works again")
                    failing = False
            stop.wait(tick_s)
