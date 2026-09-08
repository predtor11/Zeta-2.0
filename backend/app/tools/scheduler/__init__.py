"""Scheduling tools: reminders and recurring requests."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.plugins.loader import Plugin
from app.scheduler.service import LocalScheduler, job_to_dict
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolContext, ToolResult


def _parse_when(value: str) -> datetime:
    """Accept ISO datetime, 'HH:MM' (today or tomorrow), 'in 20 minutes', 'tomorrow 9:00'."""
    v = value.strip().lower()
    now = datetime.now().astimezone()
    import re

    m = re.match(r"in\s+(\d+)\s*(second|minute|hour|day)s?", v)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now + timedelta(**{unit + "s": n})
    m = re.match(r"(tomorrow\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", v)
    if m:
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if m.group(4) == "pm" and h < 12:
            h += 12
        if m.group(4) == "am" and h == 12:
            h = 0
        t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if m.group(1) or t <= now:
            t += timedelta(days=1)
        return t
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.astimezone()
    except ValueError:
        raise ValueError(f"Could not understand time '{value}'. Use ISO format, '18:00', or 'in 20 minutes'.")


class ReminderArgs(BaseModel):
    message: str = Field(description="What to remind the user about", min_length=1, max_length=500)
    when: str = Field(description="ISO datetime, 'HH:MM', '6pm', 'tomorrow 9:00', or 'in 20 minutes'")


class SetReminderTool(Tool):
    name = "set_reminder"
    description = "Set a one-off reminder that pops up in the Zeta UI at the given time."
    category = "scheduler"
    risk_level = RiskLevel.SAFE
    args_model = ReminderArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        sched: LocalScheduler = ctx.service("scheduler")
        try:
            when = _parse_when(args["when"])
        except ValueError as e:
            return ToolResult.fail(str(e))
        job = await sched.add(name=args["message"][:60], kind="reminder", payload=args["message"], schedule_type="once", run_at=when)
        return ToolResult.ok({"id": job.id, "at": when.isoformat()}, summary=f"Reminder set for {when.strftime('%a %H:%M')}")


class ScheduleArgs(BaseModel):
    name: str = Field(max_length=100)
    request: str = Field(description="Natural-language request Zeta should execute each time, e.g. 'summarize my unread emails'")
    cron: str = Field(default="", description="5-field cron in local time, e.g. '0 9 * * 1-5' (9:00 on weekdays)")
    every_minutes: int = Field(default=0, ge=0, description="Alternative to cron: run every N minutes")
    at: str = Field(default="", description="Alternative: run once at this time")


class ScheduleRequestTool(Tool):
    name = "schedule_request"
    description = "Schedule a recurring (cron / every N minutes) or one-off task that Zeta runs automatically, like 'every Monday check the project server'."
    category = "scheduler"
    risk_level = RiskLevel.SENSITIVE
    args_model = ScheduleArgs

    def describe(self, args: Dict[str, Any]) -> str:
        sched = args.get("cron") or (f"every {args.get('every_minutes')} min" if args.get("every_minutes") else args.get("at"))
        return f"Schedule '{args.get('request', '')[:60]}' ({sched})"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        sched: LocalScheduler = ctx.service("scheduler")
        try:
            if args.get("cron"):
                job = await sched.add(name=args["name"], kind="request", payload=args["request"], schedule_type="cron", cron=args["cron"])
            elif args.get("every_minutes"):
                job = await sched.add(name=args["name"], kind="request", payload=args["request"], schedule_type="interval",
                                      interval_seconds=args["every_minutes"] * 60)
            elif args.get("at"):
                job = await sched.add(name=args["name"], kind="request", payload=args["request"], schedule_type="once", run_at=_parse_when(args["at"]))
            else:
                return ToolResult.fail("Provide cron, every_minutes, or at.")
        except ValueError as e:
            return ToolResult.fail(str(e))
        return ToolResult.ok(job_to_dict(job), summary=f"Scheduled '{job.name}'")


class NoArgs(BaseModel):
    pass


class ListSchedulesTool(Tool):
    name = "list_schedules"
    description = "List reminders and scheduled tasks."
    category = "scheduler"
    risk_level = RiskLevel.READ_ONLY
    args_model = NoArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        sched: LocalScheduler = ctx.service("scheduler")
        jobs = [job_to_dict(j) for j in await sched.list()]
        return ToolResult.ok(jobs, summary=f"{len(jobs)} scheduled items")


class DeleteScheduleArgs(BaseModel):
    id: str


class DeleteScheduleTool(Tool):
    name = "delete_schedule"
    description = "Delete a reminder or scheduled task by id."
    category = "scheduler"
    risk_level = RiskLevel.SAFE
    args_model = DeleteScheduleArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        sched: LocalScheduler = ctx.service("scheduler")
        ok = await sched.delete(args["id"])
        return ToolResult.ok({"deleted": ok}, summary="Deleted" if ok else "Not found") if ok else ToolResult.fail("No such schedule")


PLUGIN = Plugin(
    name="scheduler",
    description="Reminders and scheduled/recurring requests",
    permissions=["scheduler"],
    tools=lambda s: [SetReminderTool(), ScheduleRequestTool(), ListSchedulesTool(), DeleteScheduleTool()],
)
