"""LLM provider abstraction.

All providers speak the same in-process dialect: OpenAI-style message dicts
(`role`, `content`, optional `tool_calls` / `tool_call_id`) and a list of tool
schemas in OpenAI function-calling format.  Providers translate to their own
wire formats.

Providers that cannot do native tool calling should set `supports_tools=False`;
the orchestrator then falls back to a JSON-in-text protocol (see
`app/agent/toolcall_fallback.py`).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

DeltaCallback = Callable[[str], Awaitable[None]]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def to_openai(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments, ensure_ascii=False)},
        }


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Dict[str, int] = field(default_factory=dict)
    raw: Optional[Any] = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_message(self) -> Dict[str, Any]:
        msg: Dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [tc.to_openai() for tc in self.tool_calls]
        return msg


@dataclass
class ProviderInfo:
    name: str
    model: str
    base_url: str = ""
    supports_tools: bool = True
    supports_vision: bool = False


class LLMProvider(ABC):
    """Abstract base for chat-completion providers."""

    name: str = "base"
    supports_tools: bool = True
    supports_vision: bool = False

    def __init__(self, model: str, base_url: str = "", api_key: str = "", temperature: float = 0.2,
                 max_tokens: int = 2048, timeout: int = 180):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key  # private: never logged or exposed
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    @abstractmethod
    async def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
                   *, temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                   model: Optional[str] = None) -> LLMResponse:
        """Run one chat completion."""

    async def chat_stream(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
                          *, on_delta: Optional[DeltaCallback] = None, temperature: Optional[float] = None,
                          max_tokens: Optional[int] = None, model: Optional[str] = None) -> LLMResponse:
        """Like `chat`, but calls `on_delta(text)` as visible text arrives.

        Providers without streaming inherit this default, which emits the whole
        reply once; the orchestrator therefore works identically either way.
        """
        resp = await self.chat(messages, tools, temperature=temperature, max_tokens=max_tokens, model=model)
        if on_delta and resp.content:
            await on_delta(resp.content)
        return resp

    async def embed(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]:
        """Return embeddings. Providers without embeddings raise NotImplementedError."""
        raise NotImplementedError(f"{self.name} does not support embeddings")

    async def health(self) -> Dict[str, Any]:
        """Return {"ok": bool, "detail": str, "models": [...]}. Must not raise."""
        return {"ok": True, "detail": "unknown", "models": []}

    async def list_models(self) -> List[str]:
        return []

    async def close(self) -> None:
        return None

    def info(self) -> ProviderInfo:
        return ProviderInfo(name=self.name, model=self.model, base_url=self.base_url,
                            supports_tools=self.supports_tools, supports_vision=self.supports_vision)


def image_content_part(image_b64: str, mime: str = "image/png") -> Dict[str, Any]:
    """OpenAI-style image content part. Providers convert as needed."""
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}}
