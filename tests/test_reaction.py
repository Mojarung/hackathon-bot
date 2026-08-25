"""What the bot accepts back from the model when it asks for a reaction.

This is the validation boundary: the answer arrives as free text, and anything
Telegram would refuse has to be dropped here rather than sent and rejected.
"""

from __future__ import annotations

import pytest

from hackbot.agent import reaction
from hackbot.bot.recent import Line


@pytest.mark.parametrize("emoji", ["🤡", "😴", "🖕", "👨‍💻", "🆒"])
def test_an_offered_emoji_is_accepted(emoji: str) -> None:
    assert reaction.clean(emoji) == emoji
    assert reaction.clean(f"  {emoji} ") == emoji


@pytest.mark.parametrize("raw", ["-", "- ", "", "   ", "нет подходящей реакции"])
def test_declining_and_noise_become_no_reaction(raw: str) -> None:
    assert reaction.clean(raw) is None


@pytest.mark.parametrize("emoji", ["🌚", "💤", "🦖", "❤️‍🩹"])
def test_an_emoji_telegram_would_refuse_is_dropped(emoji: str) -> None:
    """Sending one of these back would be an API error and a reaction nobody sees."""
    assert reaction.clean(emoji) is None


def test_an_answer_wrapped_in_words_still_counts() -> None:
    assert reaction.clean("Я бы поставил 🤡 сюда") == "🤡"


def test_the_prompt_carries_the_whole_catalogue() -> None:
    """The list lives in code, so the model only ever sees it through the prompt."""
    prompt = reaction.build_prompt([Line(author="Кирилл", user_id=1, text="привет")], "привет")
    for emoji in reaction.ALLOWED:
        assert emoji in prompt


def test_the_prompt_separates_context_from_the_target() -> None:
    lines = [
        Line(author="Кирилл", user_id=1, text="обсуждаем архитектуру"),
        Line(author="Саня", user_id=2, text="я задеплою в пятницу"),
    ]
    prompt = reaction.build_prompt(lines, "я задеплою в пятницу")
    assert "Кирилл: обсуждаем архитектуру" in prompt
    assert prompt.count("я задеплою в пятницу") == 1, "цель не дублируется в контексте"


async def test_an_empty_message_never_reaches_the_model() -> None:
    assert await reaction.pick_reaction([], "   ") is None
