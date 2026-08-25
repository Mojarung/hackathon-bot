"""Picking an emoji reaction that actually fits the message.

Telegram only accepts reactions from a fixed set, and a chat can narrow that set
further, so the model is given a shortlist to choose from rather than being asked
for "an emoji" - anything outside the list comes back as an API error and a
reaction nobody sees.

The answer comes back as plain text and is checked here rather than through a
Literal output type. Structured output on Ollama rides on tool calling, and these
models are shaky at it: asked for one of an enum, minimax-m3 burned both retries
on "го спать уже три часа ночи", while the same question in plain text answered
🥱 first time. Validating in code costs one call instead of three and degrades to
"no reaction" instead of an exception.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic_ai import Agent

from hackbot.agent import prompts
from hackbot.agent.defaults import DEFAULT_REACTION_PROMPT
from hackbot.agent.llm import LLMUnavailableError, fun_model, model_settings
from hackbot.bot.recent import Line

log = logging.getLogger(__name__)

SKIP = "-"
TIMEOUT_SECONDS = 20

# All of these are in Telegram's default reaction set, so they work in any chat
# that has not narrowed it down. Deliberately blunt: 👍 on everything would be
# worse than no reactions at all, and the point is that the bot has an opinion.
ALLOWED: tuple[str, ...] = (
    "🤡", "💩", "🗿", "🤨", "🥱", "🤓", "👀", "🔥", "👍", "👎", "😁", "🤣",
    "🤯", "😱", "🎉", "🏆", "💯", "🤔", "😭", "🙏", "🫡", "💔", "🖕", "😴",
    "🤝", "⚡", "🙈", "😎", "🤪", "🥴", "😢", "😡", "👏", "🤩", "🤷", "🆒",
    "👨‍💻",
)
_ALLOWED = frozenset(ALLOWED)


def catalogue() -> str:
    return " ".join(ALLOWED)


def build_prompt(lines: list[Line], target: str) -> str:
    context = "\n".join(f"{line.author}: {line.text}" for line in lines[:-1][-5:])
    parts = []
    if context:
        parts += ["Разговор до этого:", context, ""]
    parts += [
        "Сообщение, на которое ставишь реакцию:",
        target,
        "",
        f"Доступные реакции, выбирай только из них:\n{catalogue()}",
        "",
        f"Верни один символ. Если реагировать не на что - верни {SKIP}",
    ]
    return "\n".join(parts)


def clean(raw: str) -> str | None:
    """The model's answer, or None for anything that is not one of ours."""
    text = (raw or "").strip()
    if not text or text.startswith(SKIP):
        return None
    if text in _ALLOWED:
        return text
    # Models sometimes wrap the answer in a sentence; take the first thing that
    # is actually offered rather than rejecting the whole reply.
    for token in text.split():
        if token in _ALLOWED:
            return token
    log.info("реакция %r не из списка, пропускаю", text[:20])
    return None


async def pick_reaction(lines: list[Line], target: str) -> str | None:
    """One emoji for the last message, or None when nothing fits."""
    if not target.strip():
        return None
    try:
        agent = Agent(
            fun_model(),
            output_type=str,
            instructions=prompts.load("reaction", DEFAULT_REACTION_PROMPT),
            # These are reasoning models: thinking counts against max_tokens, and
            # a starved run returns nothing at all.
            model_settings=model_settings(max_tokens=1536, temperature=0.6),
            retries=1,
        )
        async with asyncio.timeout(TIMEOUT_SECONDS):
            result = await agent.run(build_prompt(lines, target))
    except (LLMUnavailableError, TimeoutError):
        return None
    except Exception as exc:
        log.warning("reaction pick failed: %s", exc)
        return None
    return clean(result.output)
