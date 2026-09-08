"""Anthropic Messages API provider (no SDK dependency; plain httpx).

Translates OpenAI-style messages/tools to the Anthropic wire format.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.core.exceptions import ProviderError, ProviderUnavailable
from app.providers.llm.base import LLMProvider, LLMResponse, ToolCall

log = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    supports_tools = True
    supports_vision = True

    def __init__(self, model: str, base_url: str = "", api_key: str = "", **kwargs):
        super().__init__(model, base_url or "https://api.anthropic.com", api_key, **kwargs)
        self._client: Optional[httpx.AsyncClient] = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout,
                headers={"x-api-key": self._api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _convert(messages: List[Dict[str, Any]]):
        system_parts: List[str] = []
        out: List[Dict[str, Any]] = []
        for m in messages:
            role = m["role"]
            content = m.get("content", "")
            if role == "system":
                system_parts.append(content if isinstance(content, str) else json.dumps(content))
                continue
            if role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": content or ""}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
                continue
            if role == "assistant":
                blocks: List[Dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in m.get("tool_calls") or []:
                    fn = tc["function"]
                    args = fn["arguments"]
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    blocks.append({"type": "tool_use", "id": tc["id"], "name": fn["name"], "input": args})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
                continue
            # user
            if isinstance(content, list):
                blocks = []
                for part in content:
                    if part.get("type") == "text":
                        blocks.append({"type": "text", "text": part["text"]})
                    elif part.get("type") == "image_url":
                        url = part["image_url"]["url"]
                        if url.startswith("data:"):
                            header, b64 = url.split(",", 1)
                            mime = header[5:].split(";")[0]
                            blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
                out.append({"role": "user", "content": blocks})
            else:
                out.append({"role": "user", "content": content or ""})
        # Anthropic requires alternating roles; merge consecutive same-role messages.
        merged: List[Dict[str, Any]] = []
        for m in out:
            if merged and merged[-1]["role"] == m["role"]:
                a, b = merged[-1]["content"], m["content"]
                a = a if isinstance(a, list) else [{"type": "text", "text": a}]
                b = b if isinstance(b, list) else [{"type": "text", "text": b}]
                merged[-1]["content"] = a + b
            else:
                merged.append(m)
        return "\n\n".join(system_parts), merged

    @staticmethod
    def _convert_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {"name": t["function"]["name"], "description": t["function"].get("description", ""),
             "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}})}
            for t in tools
        ]

    async def chat(self, messages, tools=None, *, temperature=None, max_tokens=None, model=None) -> LLMResponse:
        system, converted = self._convert(messages)
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": converted,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self._convert_tools(tools)
        try:
            r = await self.client().post("/v1/messages", json=payload)
        except httpx.ConnectError as e:
            raise ProviderUnavailable(str(e), user_message="I can't reach the Anthropic API.") from e
        except httpx.TimeoutException as e:
            raise ProviderError("timeout", user_message="The AI model took too long to respond.") from e
        if r.status_code >= 400:
            detail = r.text[:500]
            log.error("anthropic error %s: %s", r.status_code, detail)
            raise ProviderError(detail, user_message=f"The Anthropic API returned an error ({r.status_code}).")
        data = r.json()
        text_parts, calls = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(id=block["id"], name=block["name"], arguments=block.get("input") or {}))
        usage = data.get("usage") or {}
        return LLMResponse(content="".join(text_parts), tool_calls=calls, finish_reason=data.get("stop_reason", "end_turn"),
                           usage={"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)}, raw=data)

    async def chat_stream(self, messages, tools=None, *, on_delta=None, temperature=None, max_tokens=None, model=None) -> LLMResponse:
        system, converted = self._convert(messages)
        payload: Dict[str, Any] = {
            "model": model or self.model, "messages": converted, "max_tokens": max_tokens or self.max_tokens,
            "temperature": self.temperature if temperature is None else temperature, "stream": True,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self._convert_tools(tools)
        text_parts: List[str] = []
        blocks: Dict[int, Dict[str, Any]] = {}
        stop_reason = "end_turn"
        usage: Dict[str, int] = {}
        try:
            async with self.client().stream("POST", "/v1/messages", json=payload) as r:
                if r.status_code >= 400:
                    await r.aread()
                    detail = r.text[:500]
                    log.error("anthropic error %s: %s", r.status_code, detail)
                    raise ProviderError(detail, user_message=f"The Anthropic API returned an error ({r.status_code}).")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    t = ev.get("type")
                    if t == "content_block_start":
                        cb = ev.get("content_block") or {}
                        blocks[ev.get("index", 0)] = {"type": cb.get("type"), "id": cb.get("id"), "name": cb.get("name"), "json": ""}
                    elif t == "content_block_delta":
                        d = ev.get("delta") or {}
                        if d.get("type") == "text_delta":
                            text_parts.append(d.get("text", ""))
                            if on_delta and d.get("text"):
                                await on_delta(d["text"])
                        elif d.get("type") == "input_json_delta":
                            blocks.setdefault(ev.get("index", 0), {"type": "tool_use", "json": ""})["json"] += d.get("partial_json", "")
                    elif t == "message_delta":
                        stop_reason = (ev.get("delta") or {}).get("stop_reason") or stop_reason
                        usage["completion_tokens"] = (ev.get("usage") or {}).get("output_tokens", 0)
                    elif t == "message_start":
                        usage["prompt_tokens"] = ((ev.get("message") or {}).get("usage") or {}).get("input_tokens", 0)
                    elif t == "error":
                        raise ProviderError(str(ev.get("error")), user_message="The Anthropic API returned an error.")
        except httpx.ConnectError as e:
            raise ProviderUnavailable(str(e), user_message="I can't reach the Anthropic API.") from e
        except httpx.TimeoutException as e:
            raise ProviderError("timeout", user_message="The AI model took too long to respond.") from e
        calls: List[ToolCall] = []
        for i in sorted(blocks):
            b = blocks[i]
            if b.get("type") != "tool_use":
                continue
            try:
                args = json.loads(b["json"]) if b["json"].strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": b["json"]}
            calls.append(ToolCall(id=b.get("id") or f"call_{i}", name=b.get("name") or "", arguments=args))
        return LLMResponse(content="".join(text_parts), tool_calls=calls, finish_reason=stop_reason, usage=usage)

    async def health(self) -> Dict[str, Any]:
        if not self._api_key:
            return {"ok": False, "detail": "no API key", "models": []}
        return {"ok": True, "detail": "configured", "models": [self.model]}
