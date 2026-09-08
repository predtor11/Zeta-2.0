"""Meta tools the agent uses to expose its plan/progress to the UI."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from app.plugins.loader import Plugin
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolContext, ToolResult


class SetPlanArgs(BaseModel):
    steps: List[str] = Field(description="Short, user-facing steps in order (max 12)", min_length=1, max_length=12)


class SetPlanTool(Tool):
    name = "set_plan"
    description = ("Declare a short plan for a multi-step task so the user can follow progress. Call this first for tasks "
                   "needing several actions. Steps must be concise and user-facing, not internal reasoning.")
    category = "agent"
    risk_level = RiskLevel.READ_ONLY
    args_model = SetPlanArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        tasks = ctx.service("tasks")
        task = tasks.get(ctx.task_id)
        if task is None:
            return ToolResult.fail("no active task")
        steps = [s.strip()[:120] for s in args["steps"] if s.strip()]
        tasks.set_plan(task, steps)
        return ToolResult.ok({"plan": task.plan}, summary=f"Plan set with {len(steps)} steps")

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Planning {len(args.get('steps', []))} steps"


class UpdateStepArgs(BaseModel):
    step: int = Field(description="1-based step index", ge=1)
    status: str = Field(description="running | done | failed | skipped", pattern="^(running|done|failed|skipped)$")
    note: str = Field(default="", description="Optional short note", max_length=200)


class UpdateStepTool(Tool):
    name = "update_step"
    description = "Update the status of a plan step (running/done/failed/skipped) as you work through the plan."
    category = "agent"
    risk_level = RiskLevel.READ_ONLY
    args_model = UpdateStepArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        tasks = ctx.service("tasks")
        task = tasks.get(ctx.task_id)
        if task is None:
            return ToolResult.fail("no active task")
        tasks.update_step(task, args["step"], args["status"], args.get("note", ""))
        return ToolResult.ok({"step": args["step"], "status": args["status"]}, summary=f"Step {args['step']} {args['status']}")

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Step {args.get('step')} -> {args.get('status')}"


PLUGIN = Plugin(
    name="agent",
    description="Planning and progress reporting",
    permissions=["agent"],
    tools=[SetPlanTool(), UpdateStepTool()],
)
