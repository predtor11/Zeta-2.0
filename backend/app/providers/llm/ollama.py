"""Ollama native provider (`/api/chat`).

Uses the native API rather than Ollama's OpenAI shim because the native API
exposes `images` for vision models and `/api/tags` for health/model listing.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from app.core.exceptions import ProviderError, ProviderUnavailable
from app.providers.llm.base import LLMProvider, LLMResponse, ToolCall

log = logging.getLogger(__name__)


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def visible_text(raw: str) -> str:
    """Text a user should see while streaming: complete <think> blocks removed, an
    unterminated trailing <think> hidden."""
    out = _THINK_RE.sub("", raw)
    i = out.find("<think>")
    if i >= 0:
        out = out[:i]
    return out.lstrip()


class StreamAccumulator:
    """Tracks raw streamed text and yields only newly visible (non-think) text."""

    def __init__(self):
        self.raw = ""
        self._sent = ""

    def push(self, chunk: str) -> str:
        self.raw += chunk
        vis = visible_text(self.raw)
        if not vis.startswith(self._sent):
            return ""  # visible text shrank (rare); the final message corrects the UI
        tail = vis[len(self._sent):]
        # Never emit a partial "<think" tag; wait for more text.
        lt = tail.rfind("<")
        if lt >= 0 and (("<think>".startswith(tail[lt:])) or ("</think>".startswith(tail[lt:]))):
            tail = tail[:lt]
        self._sent += tail
        return tail


class OllamaProvider(LLMProvider):
    name = "ollama"
    supports_vision = True  # depends on model; images are passed through when present

    def __init__(self, model: str, base_url: str = "", api_key: str = "", supports_tools: bool = True, think: bool = False, context_length: int = 16384,
                 keep_alive: str = "30m", **kwargs):
        super().__init__(model, base_url or "http://localhost:11434", api_key, **kwargs)
        self.supports_tools = supports_tools
        self.think = think
        self.context_length = context_length
        self.keep_alive = keep_alive
        self._client: Optional[httpx.AsyncClient] = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
            # Connecting is instant or hopeless; generating is what takes time. Splitting the
            # two means a stopped Ollama is reported in seconds instead of after the full budget,
            # and that on a streaming reply the budget applies between chunks, not to the whole
            # answer - a long reply that is still arriving is not a timeout.
            timeout = httpx.Timeout(self.timeout, connect=5.0)
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, headers=headers)
        return self._client

    # ------------------------------------------------------------------ where the weights are
    async def placement(self) -> Dict[str, Any]:
        """Is the model entirely in VRAM, or is part of it running on the CPU?

        Ollama does not complain when a model does not fit: it puts the leftover layers on the
        CPU and carries on at roughly a tenth of the speed. `/api/ps` is the only place that
        difference is visible, and it is the difference between a 3-second reply and a timeout.
        """
        try:
            r = await self.client().get("/api/ps", timeout=5.0)
            models = r.json().get("models") or []
        except Exception:  # noqa: BLE001
            return {}
        for m in models:
            if m.get("model") == self.model or m.get("name") == self.model:
                total, vram = int(m.get("size") or 0), int(m.get("size_vram") or 0)
                return {"model": self.model, "total_mb": total // 2 ** 20, "vram_mb": vram // 2 ** 20,
                        "cpu_mb": max(0, total - vram) // 2 ** 20, "on_cpu": total > vram + 64 * 2 ** 20,
                        "context_length": m.get("context_length")}
        return {"model": self.model, "loaded": False}

    async def warm(self) -> Dict[str, Any]:
        """Load the model now so the first real turn is not the one that pays for it."""
        try:
            await self.client().post("/api/generate", json={"model": self.model, "keep_alive": self.keep_alive,
                                                            "options": {"num_ctx": self.context_length}})
        except Exception as e:  # noqa: BLE001
            log.debug("could not preload %s: %s", self.model, e)
        return await self.placement()

    async def unload(self) -> bool:
        """Drop the model from memory now, handing the VRAM to whatever needs it next.

        The next request reloads it from the page cache, which costs a few seconds - far less
        than one turn spent half on the CPU. Zeta only does this when the GPU is genuinely too
        full for the voice to speak.
        """
        try:
            r = await self.client().post("/api/generate", json={"model": self.model, "keep_alive": 0}, timeout=30.0)
            return r.status_code < 400
        except Exception as e:  # noqa: BLE001
            log.debug("could not unload %s: %s", self.model, e)
            return False

    async def _timed_out(self, e: Exception) -> ProviderError:
        """A timeout with the reason attached, when the reason is knowable."""
        placement = await self.placement()
        if placement.get("on_cpu"):
            detail = (f"The local model took too long. {placement['cpu_mb']} MB of {self.model} does not fit on the "
                      f"GPU and is running on the CPU, which is around ten times slower. Free some VRAM - close other "
                      f"GPU applications, or lower LLM_CONTEXT_LENGTH.")
        else:
            detail = "The local model took too long to respond."
        return ProviderError("Ollama timed out", user_message=detail)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _convert_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for m in messages:
            msg: Dict[str, Any] = {"role": m["role"]}
            content = m.get("content", "")
            if isinstance(content, list):
                texts, images = [], []
                for part in content:
                    if part.get("type") == "text":
                        texts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        url = part["image_url"]["url"]
                        images.append(url.split(",", 1)[1] if url.startswith("data:") else url)
                msg["content"] = "\n".join(texts)
                if images:
                    msg["images"] = images
            else:
                msg["content"] = content or ""
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {"function": {"name": tc["function"]["name"],
                                  "arguments": json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"]}}
                    for tc in m["tool_calls"]
                ]
            out.append(msg)
        return out

    async def chat(self, messages, tools=None, *, temperature=None, max_tokens=None, model=None) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": self._convert_messages(messages),
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "num_predict": max_tokens or self.max_tokens,
                "num_ctx": self.context_length,
            },
        }
        if tools and self.supports_tools:
            payload["tools"] = tools
        if not self.think:
            payload["think"] = False  # ignored by non-thinking models / older servers
        try:
            r = await self.client().post("/api/chat", json=payload)
        except httpx.ConnectError as e:
            raise ProviderUnavailable(f"Cannot connect to Ollama at {self.base_url}: {e}",
                                      user_message=f"I can't reach Ollama at {self.base_url}. Start it with `ollama serve`.") from e
        except httpx.TimeoutException as e:
            raise await self._timed_out(e) from e
        if r.status_code >= 400:
            detail = r.text[:500]
            if "think" in detail.lower() and "think" in payload:
                payload.pop("think", None)
                self.think = True
                return await self.chat(messages, tools, temperature=temperature, max_tokens=max_tokens, model=model)
            if r.status_code == 404 and "not found" in detail.lower():
                raise ProviderError(detail, user_message=f"Model '{payload['model']}' is not pulled. Run `ollama pull {payload['model']}`.")
            if tools and self.supports_tools and "does not support tools" in detail.lower():
                log.warning("Model %s does not support tools; using JSON fallback protocol", payload["model"])
                self.supports_tools = False
                return await self.chat(messages, None, temperature=temperature, max_tokens=max_tokens, model=model)
            raise ProviderError(detail, user_message=f"Ollama returned an error ({r.status_code}).")
        data = r.json()
        msg = data.get("message", {})
        calls: List[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args))
        usage = {"prompt_tokens": data.get("prompt_eval_count", 0), "completion_tokens": data.get("eval_count", 0)}
        content = _strip_think(msg.get("content") or "")
        return LLMResponse(content=content, tool_calls=calls,
                           finish_reason=data.get("done_reason", "stop"), usage=usage, raw=data)

    async def chat_stream(self, messages, tools=None, *, on_delta=None, temperature=None, max_tokens=None, model=None) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": self._convert_messages(messages),
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "num_predict": max_tokens or self.max_tokens,
                "num_ctx": self.context_length,
            },
        }
        if tools and self.supports_tools:
            payload["tools"] = tools
        if not self.think:
            payload["think"] = False
        acc = StreamAccumulator()
        calls: List[ToolCall] = []
        usage: Dict[str, int] = {}
        done_reason = "stop"
        try:
            async with self.client().stream("POST", "/api/chat", json=payload) as r:
                if r.status_code >= 400:
                    await r.aread()
                    # Let the non-streaming path apply its error handling / retries.
                    resp = await self.chat(messages, tools, temperature=temperature, max_tokens=max_tokens, model=model)
                    if on_delta and resp.content:
                        await on_delta(resp.content)
                    return resp
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("error"):
                        raise ProviderError(str(data["error"]), user_message=f"Ollama returned an error: {str(data['error'])[:120]}")
                    msg = data.get("message") or {}
                    chunk = msg.get("content") or ""
                    if chunk:
                        delta = acc.push(chunk)
                        if delta and on_delta:
                            await on_delta(delta)
                    for i, tc in enumerate(msg.get("tool_calls") or []):
                        fn = tc.get("function", {})
                        args = fn.get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {"_raw": args}
                        calls.append(ToolCall(id=tc.get("id") or f"call_{len(calls) + i}", name=fn.get("name", ""), arguments=args))
                    if data.get("done"):
                        usage = {"prompt_tokens": data.get("prompt_eval_count", 0), "completion_tokens": data.get("eval_count", 0)}
                        done_reason = data.get("done_reason", "stop")
        except httpx.ConnectError as e:
            raise ProviderUnavailable(f"Cannot connect to Ollama at {self.base_url}: {e}",
                                      user_message=f"I can't reach Ollama at {self.base_url}. Start it with `ollama serve`.") from e
        except httpx.TimeoutException as e:
            raise await self._timed_out(e) from e
        return LLMResponse(content=_strip_think(acc.raw), tool_calls=calls, finish_reason=done_reason, usage=usage)

    async def embed(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]:
        r = await self.client().post("/api/embed", json={"model": model or self.model, "input": texts})
        r.raise_for_status()
        return r.json().get("embeddings", [])

    async def list_models(self) -> List[str]:
        try:
            r = await self.client().get("/api/tags", timeout=10)
            r.raise_for_status()
            return [m.get("name", "") for m in r.json().get("models", [])]
        except Exception:
            return []

    async def health(self) -> Dict[str, Any]:
        try:
            r = await self.client().get("/api/tags", timeout=5)
            if r.status_code == 200:
                models = [m.get("name", "") for m in r.json().get("models", [])]
                has_model = any(m == self.model or m.split(":")[0] == self.model.split(":")[0] for m in models)
                detail = "connected" if has_model else f"connected, but model '{self.model}' is not pulled"
                return {"ok": True, "detail": detail, "models": models, "model_available": has_model}
            return {"ok": False, "detail": f"HTTP {r.status_code}", "models": []}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"unreachable ({e.__class__.__name__})", "models": []}
