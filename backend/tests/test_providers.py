"""Provider switching and wire-format translation (no network)."""

import json

import httpx
import pytest

from app.core.config import Settings
from app.providers.llm.anthropic import AnthropicProvider
from app.providers.llm.mock import MockLLMProvider
from app.providers.llm.ollama import OllamaProvider
from app.providers.llm.openai_compat import LMStudioProvider, OpenAICompatibleProvider, OpenAIProvider
from app.providers.llm.registry import PROVIDERS, build_llm_provider, register_provider
from app.providers.stt import DisabledSTT, OpenAICompatibleSTT, WhisperSTT, build_stt_provider
from app.providers.tts import DisabledTTS, ElevenLabsTTS, LocalTTS, PiperTTS, build_tts_provider


def _settings(**kw) -> Settings:
    base = dict(zeta_mode="local", fs_allowed_roots="C:/tmp")
    base.update(kw)
    return Settings(_env_file=None, **base)


def test_switch_providers_by_config():
    assert isinstance(build_llm_provider(_settings(llm_provider="ollama")), OllamaProvider)
    assert isinstance(build_llm_provider(_settings(llm_provider="lmstudio")), LMStudioProvider)
    assert isinstance(build_llm_provider(_settings(llm_provider="openai", openai_api_key="k")), OpenAIProvider)
    assert isinstance(build_llm_provider(_settings(llm_provider="anthropic", anthropic_api_key="k")), AnthropicProvider)
    assert isinstance(build_llm_provider(_settings(llm_provider="openai_compatible", llm_base_url="http://x/v1")), OpenAICompatibleProvider)
    assert isinstance(build_llm_provider(_settings(llm_provider="mock")), MockLLMProvider)


def test_config_validation_errors():
    with pytest.raises(ValueError):
        _settings(llm_provider="openai")  # missing key
    with pytest.raises(ValueError):
        _settings(llm_provider="openai_compatible") and build_llm_provider(_settings(llm_provider="openai_compatible"))
    with pytest.raises(ValueError):
        _settings(tts_provider="elevenlabs")
    with pytest.raises(ValueError):
        _settings(zeta_mode="cloud", host="0.0.0.0")


def test_voice_provider_alias():
    s = _settings(voice_provider="local")
    assert s.tts_provider.value == "local"


def test_plugin_can_register_provider():
    class Custom(MockLLMProvider):
        name = "custom"

    register_provider("custom", Custom)
    assert "custom" in PROVIDERS
    assert isinstance(build_llm_provider(_settings(), provider="custom"), Custom)


def test_stt_tts_switching():
    assert isinstance(build_stt_provider(_settings()), DisabledSTT)
    assert isinstance(build_stt_provider(_settings(stt_provider="whisper")), WhisperSTT)
    assert isinstance(build_stt_provider(_settings(stt_provider="openai", openai_api_key="k")), OpenAICompatibleSTT)
    assert isinstance(build_tts_provider(_settings()), DisabledTTS)
    assert isinstance(build_tts_provider(_settings(tts_provider="local")), LocalTTS)
    assert isinstance(build_tts_provider(_settings(tts_provider="piper")), PiperTTS)
    assert isinstance(build_tts_provider(_settings(tts_provider="elevenlabs", elevenlabs_api_key="k")), ElevenLabsTTS)


@pytest.mark.asyncio
async def test_openai_compat_parses_tool_calls():
    p = OpenAICompatibleProvider("m", "http://test/v1", "key")

    def handler(request: httpx.Request):
        assert request.headers["authorization"] == "Bearer key"
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "search_files"
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "search_files", "arguments": "{\"query\": \"x\"}"}}]},
            "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}})

    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test/v1", headers=p._headers())
    r = await p.chat([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "search_files", "parameters": {}}}])
    assert r.tool_calls[0].name == "search_files" and r.tool_calls[0].arguments == {"query": "x"}
    assert r.usage["completion_tokens"] == 2


@pytest.mark.asyncio
async def test_ollama_converts_messages_and_parses():
    p = OllamaProvider("llama3.1")
    seen = {}

    def handler(request: httpx.Request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "open_file", "arguments": {"path": "a.txt"}}}]}, "done_reason": "stop", "eval_count": 3})

    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=p.base_url)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]}]
    r = await p.chat(msgs, [{"type": "function", "function": {"name": "open_file", "parameters": {}}}])
    assert seen["messages"][1]["images"] == ["QUJD"]
    assert r.tool_calls[0].arguments == {"path": "a.txt"}


@pytest.mark.asyncio
async def test_ollama_unreachable_gives_user_message():
    from app.core.exceptions import ProviderUnavailable

    p = OllamaProvider("llama3.1", base_url="http://127.0.0.1:1")

    def handler(request):
        raise httpx.ConnectError("refused")

    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=p.base_url)
    with pytest.raises(ProviderUnavailable) as ei:
        await p.chat([{"role": "user", "content": "x"}])
    assert "ollama serve" in ei.value.user_message


def test_anthropic_message_conversion():
    system, msgs = AnthropicProvider._convert([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "f", "arguments": "{\"a\": 1}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
    ])
    assert system == "sys"
    assert msgs[1]["content"][0]["type"] == "tool_use" and msgs[1]["content"][0]["input"] == {"a": 1}
    assert msgs[2]["role"] == "user" and msgs[2]["content"][0]["type"] == "tool_result"


def test_fallback_parser():
    from app.agent.toolcall_fallback import parse_tool_calls

    calls = parse_tool_calls('Sure.\n```json\n{"tool": "search_files", "arguments": {"query": "resume"}}\n```', {"search_files"})
    assert calls and calls[0].name == "search_files"
    assert parse_tool_calls("just text", {"search_files"}) == []
    assert parse_tool_calls('{"tool": "unknown", "arguments": {}}', {"search_files"}) == []
