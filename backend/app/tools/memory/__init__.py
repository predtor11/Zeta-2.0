"""Memory tools: remember / recall / forget."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from app.plugins.loader import Plugin
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolContext, ToolResult


class RememberArgs(BaseModel):
    content: str = Field(description="The fact or preference to remember, as a complete sentence", min_length=3, max_length=2000)
    category: str = Field(default="fact", description="preference | fact | project | contact | other")
    tags: List[str] = Field(default_factory=list, description="Optional keywords")
    importance: float = Field(default=0.5, ge=0.0, le=1.0)


class RememberTool(Tool):
    name = "remember"
    description = "Store a durable fact or preference about the user in long-term memory (e.g. 'User prefers concise responses')."
    category = "memory"
    risk_level = RiskLevel.SAFE
    args_model = RememberArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        ltm = ctx.service("long_term_memory")
        item = await ltm.add(args["content"], args.get("category", "fact"), args.get("tags"), args.get("importance", 0.5))
        return ToolResult.ok({"id": item.id, "content": item.content}, summary="Remembered")

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Remember: {args.get('content', '')[:80]}"


class RecallArgs(BaseModel):
    query: str = Field(description="What to look for", min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


class RecallTool(Tool):
    name = "recall"
    description = "Search long-term memory for facts and preferences relevant to a query."
    category = "memory"
    risk_level = RiskLevel.READ_ONLY
    args_model = RecallArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        ltm = ctx.service("long_term_memory")
        items = await ltm.search(args["query"], limit=args.get("limit", 5))
        out = [{"id": i["id"], "content": i["content"], "category": i["category"], "score": i["score"]} for i in items]
        return ToolResult.ok(out, summary=f"Recalled {len(out)} memories")


class ForgetArgs(BaseModel):
    query: str = Field(default="", description="Forget every memory mentioning this topic")
    memory_id: str = Field(default="", description="Or forget one memory by id")
    everything: bool = Field(default=False, description="Forget ALL memories")


class ForgetTool(Tool):
    name = "forget"
    description = "Delete memories: by topic ('Forget everything you know about X'), by id, or everything."
    category = "memory"
    risk_level = RiskLevel.SENSITIVE
    args_model = ForgetArgs

    def classify(self, args: Dict[str, Any]) -> RiskLevel:
        return RiskLevel.DANGEROUS if args.get("everything") else RiskLevel.SENSITIVE

    def describe(self, args: Dict[str, Any]) -> str:
        if args.get("everything"):
            return "Forget ALL long-term memories"
        if args.get("memory_id"):
            return f"Forget memory {args['memory_id']}"
        return f"Forget everything about '{args.get('query', '')}'"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        ltm = ctx.service("long_term_memory")
        if args.get("everything"):
            n = await ltm.clear()
        elif args.get("memory_id"):
            n = 1 if await ltm.delete(args["memory_id"]) else 0
        elif args.get("query"):
            n = await ltm.forget_matching(args["query"])
        else:
            return ToolResult.fail("Specify a query, memory_id, or everything=true")
        return ToolResult.ok({"deleted": n}, summary=f"Forgot {n} memories")


PLUGIN = Plugin(
    name="memory",
    description="Long-term memory of user preferences and facts",
    permissions=["memory"],
    tools=[RememberTool(), RecallTool(), ForgetTool()],
    configuration={"EMBEDDING_PROVIDER": "optional: ollama|openai for semantic recall", "EMBEDDING_MODEL": "embedding model name"},
)
