"""Which provider a model call actually goes to, and when the spare key applies."""

from __future__ import annotations

import pytest
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel

from hackbot.agent import llm
from hackbot.config import get_settings

PRIMARY = "primary-key"
SPARE = "spare-key"
OTHER_PROVIDER = "https://api.featherless.ai/v1"


@pytest.fixture
def settings(monkeypatch):
    """A settings object the test can rewrite, with every model cache cleared."""
    current = get_settings()
    monkeypatch.setattr(current, "ollama_api_key", PRIMARY, raising=False)
    monkeypatch.setattr(current, "ollama_api_key_fallback", "", raising=False)
    llm.build_model.cache_clear()
    llm.resilient.cache_clear()
    yield current
    llm.build_model.cache_clear()
    llm.resilient.cache_clear()


def test_one_key_means_one_model(settings) -> None:
    assert isinstance(llm.resilient("minimax-m3"), OpenAIChatModel)


def test_a_spare_key_wraps_the_model(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ollama_api_key_fallback", SPARE, raising=False)
    llm.resilient.cache_clear()

    model = llm.resilient("minimax-m3")
    assert isinstance(model, FallbackModel)
    assert len(model.models) == 2, "основной и запасной"


def test_a_spare_identical_to_the_primary_is_not_a_spare(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ollama_api_key_fallback", PRIMARY, raising=False)
    llm.resilient.cache_clear()
    assert isinstance(llm.resilient("minimax-m3"), OpenAIChatModel)


def test_the_spare_is_not_offered_to_another_provider(settings, monkeypatch) -> None:
    """The spare is an Ollama key; sending it elsewhere doubles a failure."""
    monkeypatch.setattr(settings, "ollama_api_key_fallback", SPARE, raising=False)
    llm.resilient.cache_clear()
    assert isinstance(llm.resilient("some-model", OTHER_PROVIDER), OpenAIChatModel)


def test_missing_key_is_a_clear_error_not_a_crash(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ollama_api_key", "", raising=False)
    llm.build_model.cache_clear()
    llm.resilient.cache_clear()
    with pytest.raises(llm.LLMUnavailableError):
        llm.resilient("minimax-m3")
