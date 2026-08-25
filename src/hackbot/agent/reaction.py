"""Picking an emoji reaction that actually fits the message.

Telegram only accepts reactions from a fixed set, and a chat can narrow that set
further, so the model is given a shortlist to choose from rather than being
asked for "an emoji" - anything outside the list comes back as an API error and
a reaction nobody sees.

The shortlist is deliberately blunt: 👍 on everything would be worse than no
reactions at all, and the point is that the bot has an opinion.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from pydantic_ai import Agent

from hackbot.agent import prompts
from hackbot.agent.defaults import DEFAULT_REACTION_PROMPT
from hackbot.agent.llm import LLMUnavailableError, fun_model, model_settings
from hackbot.bot.recent import Line

log = logging.getLogger(__name__)

SKIP = "-"
TIMEOUT_SECONDS = 20

# All of these are in Telegram's default reaction set, so they work in any chat
# that has not narrowed it down.
Reaction = Literal[
    "-",
    "🤡",   # ты несёшь чушь
    "💩",   # идея дерьмо
    "🗿",   # без комментариев
    "🤨",   # сомнительно
    "🥱",   # скучно
    "🤓",   # душнила
    "👀",   # интересно, продолжай
    "🔥",   # огонь
    "👍",   # согласен
    "👎",   # не согласен
    "😁",   # смешно
    "🤣",   # очень смешно
    "🤯",   # ого
    "😱",   # ужас
    "🎉",   # повод порадоваться
    "🏆",   # молодец
    "💯",   # в точку
    "🤔",   # надо подумать
    "😭",   # больно это читать
    "🙏",   # спасибо или мольба
    "🫡",   # принято
    "💔",   # обидно
    "🖕",   # пошёл ты
]


async def pick_reaction(lines: list[Line], target: str) -> str | None:
    """One emoji for the last message, or None when nothing fits."""
    if not target.strip():
        return None
    context = "\n".join(f"{line.author}: {line.text}" for line in lines[:-1][-5:])
    prompt = "\n".join(
        (
            "Разговор до этого:" if context else "",
            context,
            "",
            "Сообщение, на которое ставишь реакцию:",
            target,
            "",
            "Выбери одну реакцию.",
        )
    ).strip()
    try:
        agent = Agent(
            fun_model(),
            output_type=Reaction,
            instructions=prompts.load("reaction", DEFAULT_REACTION_PROMPT),
            # These are reasoning models: thinking counts against max_tokens, and
            # a starved run returns nothing, which validation then rejects.
            model_settings=model_settings(max_tokens=1536, temperature=0.6),
            retries=2,
        )
        async with asyncio.timeout(TIMEOUT_SECONDS):
            result = await agent.run(prompt)
    except (LLMUnavailableError, TimeoutError):
        return None
    except Exception as exc:
        log.warning("reaction pick failed: %s", exc)
        return None

    choice = (result.output or "").strip()
    return None if choice in {SKIP, ""} else choice
