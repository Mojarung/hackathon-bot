"""Model factory for Ollama Cloud.

Ollama exposes an OpenAI-compatible surface at /v1, so pydantic-ai talks to it
through OpenAIChatModel. Two things to know about that endpoint:

* `response_format` (json_object and json_schema alike) is accepted and then
  silently ignored, so structured output must ride on tool calling, which is
  pydantic-ai's default output mode anyway.
* the accessible models are reasoning models whose thinking counts against
  max_tokens - starve them and `content` comes back empty with
  finish_reason=length.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from hackbot.config import get_settings

log = logging.getLogger(__name__)


class LLMUnavailableError(RuntimeError):
    """Raised when no API key is configured, so callers can degrade gracefully."""


@lru_cache(maxsize=8)
def build_model(
    model_name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> OpenAIChatModel:
    """Any OpenAI-compatible endpoint. Defaults to the Ollama Cloud settings."""
    settings = get_settings()
    key = api_key or settings.ollama_api_key
    if not key:
        raise LLMUnavailableError("no API key configured for the LLM")
    provider = OpenAIProvider(
        base_url=base_url or settings.ollama_base_url,
        api_key=key,
    )
    return OpenAIChatModel(model_name or settings.llm_model, provider=provider)


@lru_cache(maxsize=8)
def resilient(model_name: str, base_url: str | None = None, api_key: str | None = None) -> Model:
    """The same model behind a second API key, when one is configured.

    FallbackModel retries on ModelAPIError, which is what a spent quota (429) and
    a dead key (401) both arrive as, so the second key covers exactly the cases
    where the first one stops working rather than the model itself failing.

    The spare key belongs to Ollama, so it is only attached when the request is
    actually going to Ollama: pointing it at another provider would turn one
    broken request into two.
    """
    primary = build_model(model_name, base_url, api_key)
    settings = get_settings()
    spare = settings.ollama_api_key_fallback
    on_ollama = not base_url or base_url == settings.ollama_base_url
    if not spare or not on_ollama or spare == (api_key or settings.ollama_api_key):
        return primary
    return FallbackModel(primary, build_model(model_name, base_url, spare))


def chat_model() -> Model:
    """Tool-calling workhorse for the conversational agent.

    Can live on a different provider than the rest: the agent is the part whose
    tone people actually tune, and the model that suits a blunt persona is not
    necessarily the one that reads posters best.
    """
    settings = get_settings()
    return resilient(
        settings.llm_model,
        settings.chat_base_url or None,
        settings.chat_api_key or None,
    )


def vision_model() -> Model:
    """Only a couple of the cloud models accept images; this is one of them."""
    return resilient(get_settings().llm_vision_model)


def model_settings(*, max_tokens: int | None = None, temperature: float = 0.1
                   ) -> OpenAIChatModelSettings:
    settings = get_settings()
    return OpenAIChatModelSettings(
        max_tokens=max_tokens or settings.llm_max_tokens,
        temperature=temperature,
    )


def llm_available() -> bool:
    return get_settings().llm_enabled


def fun_model() -> Model:
    """Humour generator. Kept separate because the model that follows tool
    schemas best is not necessarily the one that writes the best Russian."""
    settings = get_settings()
    return resilient(settings.llm_fun_model or settings.llm_model)
