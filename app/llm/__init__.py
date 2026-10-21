"""LLM client factory."""

from __future__ import annotations

from ..config import Settings
from .base import LLMClient, LLMError, LLMRequest
from .mock import FAILURE_MODES, FaultInjectingClient, RulesBaselineClient, ScriptedClient

__all__ = [
    "FAILURE_MODES",
    "FaultInjectingClient",
    "LLMClient",
    "LLMError",
    "LLMRequest",
    "RulesBaselineClient",
    "ScriptedClient",
    "build_llm_client",
]


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(
            api_key=settings.anthropic_api_key or "",
            model=settings.anthropic_model,
            timeout_s=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            effort=settings.llm_effort,
        )
    if settings.llm_provider == "openai_compatible":
        from .openai_compat import OpenAICompatClient

        return OpenAICompatClient(
            api_key=settings.openai_api_key or "",
            model=settings.openai_model or "",
            base_url=settings.openai_base_url,
            json_mode=settings.openai_json_mode,
            timeout_s=settings.llm_timeout_seconds,
        )
    return RulesBaselineClient()
