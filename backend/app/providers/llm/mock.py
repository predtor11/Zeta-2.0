"""Mock provider for tests and for running the UI without any model.

Behaviour:
* If responses were queued via `queue(...)`, they are returned in order.
* Otherwise a `script` callable (messages, tools) -> LLMResponse may be used.
* Otherwise it echoes the last user message.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from app.providers.llm.base import LLMProvider, LLMResponse, ToolCall


class MockLLMProvider(LLMProvider):
    name = "mock"
    supports_tools = True

    def __init__(self, model: str = "mock", base_url: str = "", api_key: str = "", **kwargs):
        super().__init__(model, base_url, api_key, **kwargs)
        self._queue: Deque[LLMResponse] = deque()
        self.script: Optional[Callable[[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]], LLMResponse]] = None
        self.calls: List[Dict[str, Any]] = []

    def queue(self, *responses: LLMResponse) -> None:
        self._queue.extend(responses)

    def queue_text(self, text: str) -> None:
        self._queue.append(LLMResponse(content=text))

    def queue_tool_call(self, name: str, arguments: Dict[str, Any], call_id: str = "call_1", content: str = "") -> None:
        self._queue.append(LLMResponse(content=content, tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)], finish_reason="tool_calls"))

    async def chat(self, messages, tools=None, *, temperature=None, max_tokens=None, model=None) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools})
        if self._queue:
            return self._queue.popleft()
        if self.script:
            return self.script(messages, tools)
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        text = last_user.get("content", "") if last_user else ""
        if isinstance(text, list):
            text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
        return LLMResponse(content=f"[mock] You said: {text}")

    async def chat_stream(self, messages, tools=None, *, on_delta=None, temperature=None, max_tokens=None, model=None) -> LLMResponse:
        resp = await self.chat(messages, tools, temperature=temperature, max_tokens=max_tokens, model=model)
        if on_delta and resp.content:
            words = resp.content.split(" ")
            for i in range(0, len(words), 3):
                chunk = " ".join(words[i:i + 3])
                await on_delta(chunk + (" " if i + 3 < len(words) else ""))
        return resp

    async def embed(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]:
        # Deterministic pseudo-embedding based on character histogram (good enough for tests)
        out = []
        for t in texts:
            vec = [0.0] * 64
            for ch in t.lower():
                vec[ord(ch) % 64] += 1.0
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out

    async def health(self) -> Dict[str, Any]:
        return {"ok": True, "detail": "mock provider", "models": ["mock"]}

    async def list_models(self) -> List[str]:
        return ["mock"]
