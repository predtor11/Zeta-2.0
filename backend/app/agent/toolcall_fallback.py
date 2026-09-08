"""JSON-in-text tool-call protocol for models without native tool support."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Optional

from app.providers.llm.base import ToolCall

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _candidates(text: str) -> List[str]:
    out: List[str] = []
    for m in _FENCE_RE.finditer(text):
        out.append(m.group(1))
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        out.append(stripped)
    # first balanced {...} block in the text
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[start:i + 1])
                    break
    return out


def parse_tool_calls(text: str, known_tools: Optional[set] = None) -> List[ToolCall]:
    """Extract tool calls of the form {"tool": name, "arguments": {...}} (also accepts "name"/"parameters")."""
    if not text or "{" not in text:
        return []
    for cand in _candidates(text):
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("tool") or obj.get("name") or (obj.get("function") or {}).get("name")
        if not name or not isinstance(name, str):
            continue
        if known_tools is not None and name not in known_tools:
            continue
        args = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {}
        return [ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=name, arguments=args)]
    return []


def tools_text(schemas: List[Dict[str, Any]]) -> str:
    lines = []
    for s in schemas:
        fn = s["function"]
        props = fn.get("parameters", {}).get("properties", {})
        required = set(fn.get("parameters", {}).get("required", []))
        params = ", ".join(f"{k}{'' if k in required else '?'}: {v.get('type', 'any')}" for k, v in props.items())
        lines.append(f"- {fn['name']}({params}): {fn.get('description', '')}")
    return "\n".join(lines)
