"""Butting into a conversation nobody addressed to the bot.

Deliberately not the ReAct agent. This runs on messages that were never meant
for the bot, so it gets no tools, touches no data and cannot change anything - at
worst it says something stupid. It also only ever sees text: attachments are read
only when someone actually asks the bot to read them.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic_ai import Agent

from hackbot.agent import lore, prompts
from hackbot.agent.defaults import DEFAULT_BANTER, DEFAULT_PERSONA, SKIP_TOKEN
from hackbot.agent.llm import LLMUnavailableError, chat_model, model_settings
from hackbot.bot.recent import Line

log = logging.getLogger(__name__)

# A butt-in longer than this is not a butt-in, it is a speech.
MAX_REPLY_CHARS = 400
# Nobody asked for this reply, so a slow one is worse than none: it would land
# under a message the chat scrolled past minutes ago.
TIMEOUT_SECONDS = 30


def build_prompt(
    lines: list[Line], profiles: dict[int, str], bot_name: str, bot_id: int
) -> str:
    # Only this bot is the one talking. Другие боты в чате - обычные
    # собеседники, и пометить их реплики как свои значило бы отвечать себе.
    conversation = "\n".join(
        f"{'ТЫ' if line.user_id == bot_id else line.author}: {line.text}" for line in lines
    )
    parts = [f"Последние сообщения в чате (ты - {bot_name}):", "", conversation]
    known = [f"- {text}" for text in profiles.values() if text]
    if known:
        parts += ["", "Что ты знаешь про этих людей:", *known]
    parts += ["", "Влезь в разговор одной фразой."]
    return "\n".join(parts)


async def make_banter(
    lines: list[Line], profiles: dict[int, str], bot_name: str, bot_id: int
) -> str | None:
    """One unprompted line, or None when the model would rather stay quiet."""
    if not lines:
        return None
    # Lore matters more here than anywhere else: an unprompted line is judged
    # entirely on whether it sounds like someone who belongs in this chat.
    instructions = "\n\n".join(
        part
        for part in (
            prompts.load("persona", DEFAULT_PERSONA),
            lore.compact(),
            prompts.load("banter", DEFAULT_BANTER),
        )
        if part
    )
    try:
        agent = Agent(
            chat_model(),
            output_type=str,
            instructions=instructions,
            model_settings=model_settings(max_tokens=1024, temperature=0.9),
            retries=1,
        )
        async with asyncio.timeout(TIMEOUT_SECONDS):
            result = await agent.run(build_prompt(lines, profiles, bot_name, bot_id))
    except (LLMUnavailableError, TimeoutError):
        return None
    except Exception as exc:
        log.warning("banter generation failed: %s", exc)
        return None
    return clean(result.output)


def clean(raw: str) -> str | None:
    """Drop the skip token, the model's own stage directions and anything too long."""
    text = " ".join((raw or "").split())
    if not text:
        return None
    # Models like to answer "ТЫ: реплика" when the prompt is a transcript.
    for prefix in ("ТЫ:", "Ты:", "ты:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if not text or SKIP_TOKEN in text.upper():
        return None
    if len(text) > MAX_REPLY_CHARS:
        return None
    return text
