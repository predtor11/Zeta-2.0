"""OpenAI-compatible chat completions provider.

Covers: OpenAI, LM Studio, vLLM, llama.cpp server, OpenRouter, Groq, Together,
and Ollama's `/v1` endpoint.  Anything that speaks `/chat/completions`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from app.core.exceptions import ProviderError, ProviderUnavailable
from app.providers.llm.base import LLMProvider, LLMResponse, ToolCall
from app.providers.llm.ollama import StreamAccumulator, _strip_think

log = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

    def __init__(self, *args, supports_tools: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.supports_tools = supports_tools
        self._client: Optional[httpx.AsyncClient] = None

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, headers=self._headers(), timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def chat(self, messages, tools=None, *, temperature=None, max_tokens=None, model=None) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if tools and self.supports_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            r = await self.client().post("/chat/completions", json=payload)
        except httpx.ConnectError as e:
            raise ProviderUnavailable(f"Cannot connect to {self.name} at {self.base_url}: {e}",
                                      user_message=f"I can't reach the {self.name} server at {self.base_url}. Is it running?") from e
        except httpx.TimeoutException as e:
            raise ProviderError(f"{self.name} timed out", user_message="The AI model took too long to respond.") from e
        if r.status_code >= 400:
            detail = r.text[:500]
            log.error("%s error %s: %s", self.name, r.status_code, detail)
            if r.status_code in (401, 403):
                raise ProviderError(detail, user_message=f"The {self.name} API rejected the credentials.")
            if r.status_code == 404:
                raise ProviderError(detail, user_message=f"Model '{payload['model']}' was not found on {self.name}.")
            # Some servers reject the tools field; retry once without tools (fallback protocol takes over)
            if tools and self.supports_tools and ("tool" in detail.lower() or r.status_code == 400):
                log.warning("%s rejected tool schema; disabling native tools for this provider", self.name)
                self.supports_tools = False
                return await self.chat(messages, None, temperature=temperature, max_tokens=max_tokens, model=model)
            raise ProviderError(detail, user_message=f"The {self.name} API returned an error ({r.status_code}).")
        data = r.json()
        return self._parse(data)

    def _parse(self, data: Dict[str, Any]) -> LLMResponse:
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as e:
            raise ProviderError(f"Malformed response: {data}") from e
        msg = choice.get("message", {})
        content = msg.get("content") or ""
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()
        calls: List[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args_raw = fn.get("arguments", "{}")
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw) if args_raw.strip() else {}
                except json.JSONDecodeError:
                    args = {"_raw": args_raw}
            else:
                args = args_raw or {}
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args))
        usage = data.get("usage") or {}
        return LLMResponse(content=content, tool_calls=calls, finish_reason=choice.get("finish_reason", "stop"),
                           usage={k: v for k, v in usage.items() if isinstance(v, int)}, raw=data)

    async def chat_stream(self, messages, tools=None, *, on_delta=None, temperature=None, max_tokens=None, model=None) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": True,
        }
        if tools and self.supports_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        acc = StreamAccumulator()
        partial: Dict[int, Dict[str, str]] = {}   # index -> {id, name, arguments}
        finish = "stop"
        usage: Dict[str, int] = {}
        try:
            async with self.client().stream("POST", "/chat/completions", json=payload) as r:
                if r.status_code >= 400:
                    await r.aread()
                    resp = await self.chat(messages, tools, temperature=temperature, max_tokens=max_tokens, model=model)
                    if on_delta and resp.content:
                        await on_delta(resp.content)
                    return resp
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if not body or body == "[DONE]":
                        continue
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    if data.get("usage"):
                        usage = {k: v for k, v in data["usage"].items() if isinstance(v, int)}
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content") or ""
                        if isinstance(text, list):
                            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
                        if text:
                            vis = acc.push(text)
                            if vis and on_delta:
                                await on_delta(vis)
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", len(partial))
                            slot = partial.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        except httpx.ConnectError as e:
            raise ProviderUnavailable(f"Cannot connect to {self.name} at {self.base_url}: {e}",
                                      user_message=f"I can't reach the {self.name} server at {self.base_url}. Is it running?") from e
        except httpx.TimeoutException as e:
            raise ProviderError(f"{self.name} timed out", user_message="The AI model took too long to respond.") from e
        calls: List[ToolCall] = []
        for i in sorted(partial):
            slot = partial[i]
            raw_args = slot["arguments"]
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": raw_args}
            calls.append(ToolCall(id=slot["id"] or f"call_{i}", name=slot["name"], arguments=args))
        return LLMResponse(content=_strip_think(acc.raw), tool_calls=calls, finish_reason=finish, usage=usage)

    async def embed(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]:
        r = await self.client().post("/embeddings", json={"model": model or self.model, "input": texts})
        r.raise_for_status()
        data = r.json()
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]

    async def list_models(self) -> List[str]:
        try:
            r = await self.client().get("/models", timeout=10)
            r.raise_for_status()
            return [m.get("id", "") for m in r.json().get("data", [])]
        except Exception:
            return []

    async def health(self) -> Dict[str, Any]:
        try:
            r = await self.client().get("/models", timeout=5)
            if r.status_code == 200:
                models = [m.get("id", "") for m in r.json().get("data", [])]
                return {"ok": True, "detail": f"connected ({len(models)} models)", "models": models}
            return {"ok": r.status_code < 500, "detail": f"HTTP {r.status_code}", "models": []}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"unreachable: {e.__class__.__name__}", "models": []}


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"
    supports_vision = True

    def __init__(self, model: str, base_url: str = "", api_key: str = "", **kwargs):
        super().__init__(model, base_url or "https://api.openai.com/v1", api_key, **kwargs)


class LMStudioProvider(OpenAICompatibleProvider):
    name = "lmstudio"

    def __init__(self, model: str, base_url: str = "", api_key: str = "", **kwargs):
        super().__init__(model, base_url or "http://localhost:1234/v1", api_key or "lm-studio", **kwargs)


class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter (https://openrouter.ai): one key, hundreds of models, OpenAI-compatible API.

    Model ids look like `openai/gpt-4o-mini`, `google/gemini-2.5-flash`, `anthropic/claude-sonnet-4`,
    `meta-llama/llama-3.3-70b-instruct`; free-tier models end with `:free`.
    """

    name = "openrouter"
    supports_vision = True

    def __init__(self, model: str, base_url: str = "", api_key: str = "", **kwargs):
        super().__init__(model, base_url or "https://openrouter.ai/api/v1", api_key, **kwargs)

    def _headers(self) -> Dict[str, str]:
        h = super()._headers()
        h["HTTP-Referer"] = "https://github.com/zeta-agent"  # attribution headers recommended by OpenRouter
        h["X-Title"] = "Zeta"
        return h

    async def health(self) -> Dict[str, Any]:
        if not self._api_key:
            return {"ok": False, "detail": "no API key", "models": []}
        base = await super().health()
        if base.get("ok"):
            base["model_available"] = self.model in base.get("models", []) or not base.get("models")
            if not base["model_available"]:
                base["detail"] = f"connected, but model '{self.model}' was not found in OpenRouter's list"
        return base
