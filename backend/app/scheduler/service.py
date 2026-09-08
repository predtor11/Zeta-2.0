"""Scheduler abstraction.

`LocalScheduler` persists jobs in SQLite and ticks every few seconds inside
the FastAPI process.  It supports one-off reminders, fixed intervals and a
small cron subset (`min hour dom mon dow`, with `*`, lists and `*/n`).

Swappable for APScheduler, Windows Task Scheduler or a cloud scheduler by
implementing the same interface.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy import select

from app.core.database import session_scope
from app.core.events import event_bus
from app.models.db import ScheduledJob

log = logging.getLogger(__name__)

JobRunner = Callable[[ScheduledJob], Awaitable[None]]


def _cron_match(field: str, value: int) -> bool:
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            return True
        if part.startswith("*/"):
            step = int(part[2:])
            if step > 0 and value % step == 0:
                return True
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            if int(lo) <= value <= int(hi):
                return True
            continue
        if part.isdigit() and int(part) == value:
            return True
    return False


def cron_next(expr: str, after: datetime) -> Optional[datetime]:
    """Next local datetime matching a 5-field cron expression (searches up to 366 days)."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("cron expression must have 5 fields: minute hour day month weekday")
    minute, hour, dom, mon, dow = parts
    t = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    end = after + timedelta(days=366)
    while t < end:
        if (_cron_match(mon, t.month) and _cron_match(dom, t.day) and _cron_match(dow, t.isoweekday() % 7)
                and _cron_match(hour, t.hour) and _cron_match(minute, t.minute)):
            return t
        t += timedelta(minutes=1)
    return None


def compute_next(job: ScheduledJob, now: datetime) -> Optional[datetime]:
    if job.schedule_type == "once":
        return job.run_at if job.run_at and job.run_at > now else None
    if job.schedule_type == "interval" and job.interval_seconds:
        base = job.last_run or now
        return base + timedelta(seconds=job.interval_seconds)
    if job.schedule_type == "cron" and job.cron:
        local_now = now.astimezone()
        nxt = cron_next(job.cron, local_now.replace(tzinfo=None))
        return nxt.replace(tzinfo=local_now.tzinfo).astimezone(timezone.utc) if nxt else None
    return None


class LocalScheduler:
    def __init__(self, runner: JobRunner, tick_seconds: int = 15):
        self.runner = runner
        self.tick = tick_seconds
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._run_due()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick)
            except asyncio.TimeoutError:
                pass

    async def _run_due(self) -> None:
        now = datetime.now(timezone.utc)
        async with session_scope() as s:
            rows = (await s.execute(select(ScheduledJob).where(ScheduledJob.enabled == True, ScheduledJob.next_run <= now))).scalars().all()  # noqa: E712
            due = list(rows)
            for job in due:
                job.last_run = now
                nxt = compute_next(job, now)
                job.next_run = nxt
                if nxt is None:
                    job.enabled = False
        for job in due:
            log.info("Running scheduled job %s (%s)", job.name, job.kind)
            try:
                await self.runner(job)
            except Exception:  # noqa: BLE001
                log.exception("scheduled job %s failed", job.id)

    # ---- CRUD -------------------------------------------------------------
    async def add(self, *, name: str, kind: str, payload: str, schedule_type: str, run_at: Optional[datetime] = None,
                  interval_seconds: Optional[int] = None, cron: Optional[str] = None) -> ScheduledJob:
        if schedule_type not in ("once", "interval", "cron"):
            raise ValueError("schedule_type must be once|interval|cron")
        if kind not in ("reminder", "request"):
            raise ValueError("kind must be reminder|request")
        if schedule_type == "once" and run_at is None:
            raise ValueError("run_at is required for one-off jobs")
        if schedule_type == "interval" and not interval_seconds:
            raise ValueError("interval_seconds is required for interval jobs")
        if schedule_type == "cron":
            if not cron:
                raise ValueError("cron is required for cron jobs")
            cron_next(cron, datetime.now())  # validate
        if run_at and run_at.tzinfo is None:
            run_at = run_at.astimezone()
        job = ScheduledJob(name=name, kind=kind, payload=payload, schedule_type=schedule_type, run_at=run_at,
                           interval_seconds=interval_seconds, cron=cron, enabled=True)
        now = datetime.now(timezone.utc)
        job.next_run = run_at if schedule_type == "once" else compute_next(job, now)
        async with session_scope() as s:
            s.add(job)
            await s.flush()
        return job

    async def list(self) -> List[ScheduledJob]:
        async with session_scope() as s:
            return list((await s.execute(select(ScheduledJob).order_by(ScheduledJob.next_run.asc().nulls_last()))).scalars().all())

    async def delete(self, job_id: str) -> bool:
        async with session_scope() as s:
            job = await s.get(ScheduledJob, job_id)
            if job is None:
                return False
            await s.delete(job)
            return True

    async def set_enabled(self, job_id: str, enabled: bool) -> bool:
        async with session_scope() as s:
            job = await s.get(ScheduledJob, job_id)
            if job is None:
                return False
            job.enabled = enabled
            if enabled and job.next_run is None:
                job.next_run = compute_next(job, datetime.now(timezone.utc))
            return True


def job_to_dict(j: ScheduledJob) -> Dict[str, Any]:
    return {"id": j.id, "name": j.name, "kind": j.kind, "payload": j.payload, "schedule_type": j.schedule_type,
            "run_at": j.run_at, "interval_seconds": j.interval_seconds, "cron": j.cron, "enabled": j.enabled,
            "last_run": j.last_run, "next_run": j.next_run}


def notify_reminder(job: ScheduledJob) -> None:
    event_bus.publish("notification", title="Reminder", message=job.payload, job_id=job.id, level="info")
