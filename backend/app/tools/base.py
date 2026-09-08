"""Tool framework.

A `Tool` has structured metadata (name, description, risk level, JSON-schema
parameters) so the LLM can choose it, plus a `run()` coroutine.  Arguments are
validated with a Pydantic model before execution; tools may refine their risk
dynamically (e.g. the terminal tool classifies each command).

`ToolResult.untrusted=True` marks output that originates from external content
(web pages, emails, documents).  The orchestrator wraps such output in explicit
trust-boundary markers before it reaches the model.
"""

from __future__ import annotations

import inspect
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Type

from pydantic import BaseModel, ValidationError

from app.core.exceptions import ToolValidationError
from app.security.permissions import RiskLevel

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.security.permissions import PermissionManager
    from app.security.secrets import SecretStore

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Everything a tool may need at execution time. Built by the orchestrator."""

    settings: "Settings"
    secrets: "SecretStore"
    permissions: "PermissionManager"
    task_id: Optional[str] = None
    conversation_id: Optional[str] = None
    services: Dict[str, Any] = field(default_factory=dict)  # shared singletons: browser, index, memory, scheduler...
    emit: Any = None  # callable(message: str, **extra) for activity updates

    def activity(self, message: str, **extra: Any) -> None:
        if self.emit:
            try:
                self.emit(message, **extra)
            except Exception:  # noqa: BLE001
                log.debug("activity emit failed", exc_info=True)

    def service(self, name: str) -> Any:
        svc = self.services.get(name)
        if svc is None:
            raise RuntimeError(f"Service '{name}' is not available")
        return svc


@dataclass
class ToolResult:
    success: bool
    output: Any = None
    error: Optional[str] = None
    untrusted: bool = False          # output contains external content
    source: str = ""                 # e.g. "web", "email", "file"
    summary: str = ""                # one-line human summary for the activity panel
    artifacts: Dict[str, Any] = field(default_factory=dict)  # e.g. screenshot path, download path

    @classmethod
    def ok(cls, output: Any = None, summary: str = "", **kw: Any) -> "ToolResult":
        return cls(success=True, output=output, summary=summary, **kw)

    @classmethod
    def fail(cls, error: str, output: Any = None, **kw: Any) -> "ToolResult":
        return cls(success=False, output=output, error=error, summary=error, **kw)

    @classmethod
    def not_implemented(cls, what: str) -> "ToolResult":
        return cls.fail(f"NOT IMPLEMENTED: {what}")

    def to_llm_text(self, max_chars: int = 12000) -> str:
        if self.success:
            body = self.output
        else:
            body = {"error": self.error, **({"output": self.output} if self.output is not None else {})}
        if isinstance(body, str):
            text = body
        else:
            try:
                text = json.dumps(body, ensure_ascii=False, default=str, indent=None)
            except Exception:  # noqa: BLE001
                text = str(body)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n…[truncated {len(text) - max_chars} chars]"
        return text

    def to_dict(self) -> Dict[str, Any]:
        return {"success": self.success, "output": self.output, "error": self.error, "untrusted": self.untrusted,
                "source": self.source, "summary": self.summary, "artifacts": self.artifacts}


class Tool(ABC):
    """Base class for all tools."""

    name: str = ""
    description: str = ""
    category: str = "general"       # permission category / plugin name
    risk_level: RiskLevel = RiskLevel.SAFE
    requires_confirmation: bool = False
    args_model: Optional[Type[BaseModel]] = None
    parameters: Optional[Dict[str, Any]] = None  # explicit JSON schema (else derived from args_model)
    timeout_seconds: Optional[int] = None
    log_arguments: bool = True      # False for tools whose args may contain sensitive text
    enabled: bool = True
    available_in_cloud: bool = True  # False for tools needing the host OS (computer control)

    # ---- metadata --------------------------------------------------------
    def schema(self) -> Dict[str, Any]:
        params = self.parameters
        if params is None and self.args_model is not None:
            params = self.args_model.model_json_schema()
            params.pop("title", None)
            for prop in params.get("properties", {}).values():
                prop.pop("title", None)
        if params is None:
            params = {"type": "object", "properties": {}}
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": params}}

    def info(self, permissions: Optional["PermissionManager"] = None) -> Dict[str, Any]:
        enabled = self.enabled
        if permissions is not None:
            enabled = enabled and permissions.is_category_enabled(self.category) and self.name not in permissions.disabled_tools
        return {
            "name": self.name, "description": self.description, "category": self.category,
            "risk_level": self.risk_level.value, "requires_confirmation": self.requires_confirmation,
            "enabled": enabled, "parameters": self.schema()["function"]["parameters"],
        }

    # ---- validation / classification -------------------------------------
    def validate(self, args: Dict[str, Any]) -> Dict[str, Any]:
        args = dict(args or {})
        args.pop("_raw", None)
        if self.args_model is None:
            return args
        try:
            model = self.args_model.model_validate(args)
        except ValidationError as e:
            errs = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
            raise ToolValidationError(f"Invalid arguments for {self.name}: {errs}",
                                      user_message=f"I tried to use {self.name} with invalid arguments ({errs}).") from e
        return model.model_dump()

    def classify(self, args: Dict[str, Any]) -> RiskLevel:
        """Risk for this specific invocation. Override for dynamic classification."""
        return self.risk_level

    def describe(self, args: Dict[str, Any]) -> str:
        """Human-readable action description for confirmations / activity log."""
        pretty = ", ".join(f"{k}={v!r}" for k, v in args.items() if not str(k).startswith("_"))
        return f"{self.name}({pretty})"

    def action_key(self, args: Dict[str, Any]) -> str:
        try:
            return f"{self.name}:{json.dumps(args, sort_keys=True, default=str)}"
        except Exception:  # noqa: BLE001
            return f"{self.name}:{args!r}"

    def confirmation_details(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Extra details for the confirmation modal (e.g. list of files to delete)."""
        return dict(args)

    async def preview(self, args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        """Optional async preview computed before asking for confirmation (e.g. count files)."""
        return {}

    # ---- execution -------------------------------------------------------
    @abstractmethod
    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        ...

    async def health_check(self) -> Dict[str, Any]:
        return {"ok": True}


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError(f"Tool {tool.__class__.__name__} has no name")
        if tool.name in self._tools:
            log.warning("Tool %s registered twice; replacing", tool.name)
        self._tools[tool.name] = tool

    def register_many(self, tools: List[Tool]) -> None:
        for t in tools:
            self.register(t)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def names(self) -> List[str]:
        return list(self._tools)

    def by_category(self, category: str) -> List[Tool]:
        return [t for t in self._tools.values() if t.category == category]

    def categories(self) -> List[str]:
        return sorted({t.category for t in self._tools.values()})

    def enabled(self, permissions: Optional["PermissionManager"] = None, cloud: bool = False) -> List[Tool]:
        out = []
        for t in self._tools.values():
            if not t.enabled:
                continue
            if cloud and not t.available_in_cloud:
                continue
            if permissions is not None and (not permissions.is_category_enabled(t.category) or t.name in permissions.disabled_tools):
                continue
            out.append(t)
        return out

    def schemas(self, permissions: Optional["PermissionManager"] = None, cloud: bool = False) -> List[Dict[str, Any]]:
        return [t.schema() for t in self.enabled(permissions, cloud)]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


def run_sync(func, *args, **kwargs):
    """Helper for tools wrapping blocking code: run in the default executor."""
    import asyncio
    import functools

    loop = asyncio.get_running_loop()
    return loop.run_in_executor(None, functools.partial(func, *args, **kwargs))


def is_coroutine_fn(fn) -> bool:
    return inspect.iscoroutinefunction(fn)
