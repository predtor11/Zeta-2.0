"""Minimal external plugin: adds a `say_hello` tool.

Copy this folder to create your own plugin (Spotify, Calendar, Notion, smart home...).
See docs/PLUGINS.md.
"""

from typing import Any, Dict

from pydantic import BaseModel, Field

from app.plugins.loader import Plugin
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolContext, ToolResult


class HelloArgs(BaseModel):
    name: str = Field(default="world", description="Who to greet")


class SayHelloTool(Tool):
    name = "say_hello"
    description = "Example plugin tool that returns a greeting."
    category = "example"
    risk_level = RiskLevel.READ_ONLY
    args_model = HelloArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({"greeting": f"Hello, {args['name']}!"}, summary="Greeted")


async def health() -> Dict[str, Any]:
    return {"ok": True, "detail": "example plugin loaded"}


PLUGIN = Plugin(
    name="example",
    description="Example external plugin",
    permissions=["example"],
    tools=[SayHelloTool()],
    configuration={},
    health_check=health,
)
