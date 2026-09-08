"""Factory that builds the configured LLM provider.

Switching providers is a configuration change (LLM_PROVIDER / LLM_MODEL),
never a code change.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Type

from app.core.config import LLMProviderName, Settings
from app.providers.llm.anthropic import AnthropicProvider
from app.providers.llm.base import LLMProvider
from app.providers.llm.mock import MockLLMProvider
from app.providers.llm.ollama import OllamaProvider
from app.providers.llm.openai_compat import LMStudioProvider, OpenAICompatibleProvider, OpenAIProvider, OpenRouterProvider

log = logging.getLogger(__name__)

PROVIDERS: Dict[str, Type[LLMProvider]] = {
    LLMProviderName.OLLAMA.value: OllamaProvider,
    LLMProviderName.LMSTUDIO.value: LMStudioProvider,
    LLMProviderName.OPENAI.value: OpenAIProvider,
    LLMProviderName.OPENAI_COMPATIBLE.value: OpenAICompatibleProvider,
    LLMProviderName.OPENROUTER.value: OpenRouterProvider,
    LLMProviderName.ANTHROPIC.value: AnthropicProvider,
    LLMProviderName.MOCK.value: MockLLMProvider,
}


def register_provider(name: str, cls: Type[LLMProvider]) -> None:
    """Plugins may register additional providers."""
    PROVIDERS[name] = cls


def build_llm_provider(settings: Settings, *, provider: Optional[str] = None, model: Optional[str] = None) -> LLMProvider:
    name = (provider or settings.llm_provider.value).lower()
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"Unknown LLM provider '{name}'. Known: {', '.join(PROVIDERS)}")

    common = dict(
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=settings.llm_timeout_seconds,
    )
    model = model or settings.llm_model
    if name == "ollama":
        return OllamaProvider(model, settings.llm_base_url or settings.ollama_base_url, settings.llm_api_key,
                              supports_tools=settings.llm_supports_tools, think=settings.llm_think, context_length=settings.llm_context_length, **common)
    if name == "lmstudio":
        return LMStudioProvider(model, settings.llm_base_url or settings.lmstudio_base_url, settings.llm_api_key,
                                supports_tools=settings.llm_supports_tools, **common)
    if name == "openai":
        return OpenAIProvider(model, settings.llm_base_url, settings.openai_api_key or settings.llm_api_key, **common)
    if name == "openai_compatible":
        if not settings.llm_base_url:
            raise ValueError("LLM_BASE_URL is required for LLM_PROVIDER=openai_compatible")
        return OpenAICompatibleProvider(model, settings.llm_base_url, settings.llm_api_key,
                                        supports_tools=settings.llm_supports_tools, **common)
    if name == "openrouter":
        return OpenRouterProvider(model, settings.llm_base_url, settings.openrouter_api_key or settings.llm_api_key,
                                  supports_tools=settings.llm_supports_tools, **common)
    if name == "anthropic":
        return AnthropicProvider(model, settings.llm_base_url, settings.anthropic_api_key or settings.llm_api_key, **common)
    return cls(model, settings.llm_base_url, settings.llm_api_key, **common)


def build_embedding_provider(settings: Settings) -> Optional[LLMProvider]:
    """Returns a provider used only for `embed()`, or None if embeddings are disabled."""
    if not settings.embedding_provider:
        return None
    name = settings.embedding_provider.lower()
    if name == "mock":
        return MockLLMProvider(settings.embedding_model)
    if name == "ollama":
        return OllamaProvider(settings.embedding_model, settings.ollama_base_url, settings.llm_api_key)
    if name == "openai":
        return OpenAIProvider(settings.embedding_model, "", settings.openai_api_key or settings.llm_api_key)
    if name == "openai_compatible":
        return OpenAICompatibleProvider(settings.embedding_model, settings.llm_base_url, settings.llm_api_key)
    log.warning("Unknown EMBEDDING_PROVIDER %s; embeddings disabled", name)
    return None
